"""Bind the microphone the operator is actually near.

The constraint this answers
---------------------------
Measured across this investigation, on one machine and one voice:

    known-good speech (transcribes)   crest 15.8dB   modulation 0.472
    the lid array at seating distance crest 27.9dB   modulation 0.208

Crest factor is a RATIO, so no gain stage can move it — 20·log10(k·peak /
k·rms) is the same number for every k, which is why raising input volume from
40 to 85 lifted level by 16dB and left crest exactly where it was. What high
crest measures is the room: sustained vowels decaying into the floor while
consonant transients survive. The only variable that changes it is the
distance between the mouth and the transducer.

So this does not filter, boost, or dereverberate. It picks a closer
microphone.

What it is NOT allowed to do
----------------------------
``DeviceConfig.prefer_local_mic`` (v295.1) deliberately skips Continuity and
Bluetooth inputs so macOS never opens a Continuity handshake unbidden. That is
a real policy with a real reason, and this manager does not get to ignore it
because a heuristic feels confident. It overrides that default in exactly one
circumstance: a MEASURED score, from audio captured on the candidate device
itself, that beats the incumbent by a margin. Evidence overrides policy;
enthusiasm does not.

Design
------
* Scoring is ``acoustic_quality.rank_devices`` / ``best_device``, imported
  whole. No second copy of the ranking rules, and the margin logic that
  prevents thrashing already lives there.
* Probing happens WHILE THE OPERATOR IS SPEAKING. A microphone scored against
  an empty room is scored against nothing; every mic in a room hears the same
  voice, so a live utterance is the only fair comparison available. The
  manager therefore arms on degradation and probes on the next speech.
* The incumbent is never re-probed. Its score comes from the live rejection
  telemetry the pipeline already produces, so the swap costs one bounded
  capture on the challengers and no contention on the device in use.
* Every failure path ends at the built-in array.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import numpy as np

from backend.audio.acoustic_quality import (
    DeviceScore,
    QualitySample,
    best_device,
    rank_devices,
)

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes", "on")


def adaptive_input_enabled() -> bool:
    """Master gate, default OFF.

    Off by default deliberately: this reaches for CoreAudio and can open a
    Continuity handshake, and the handoff records three wedged processes from
    tearing down a contended input stream. It is armed by an operator who
    wants it, not by importing the module."""
    return os.getenv(
        "JARVIS_ADAPTIVE_INPUT", "0",
    ).strip().lower() in _TRUTHY


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        return max(lo, min(hi, float(os.getenv(name, "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, "").strip() or default))
    except (TypeError, ValueError):
        return default


#: Crest above which the incumbent is considered too distant. Clean speech is
#: 15-21dB; 22 leaves headroom over the top of that band without firing on a
#: single peaky syllable.
CREST_TRIGGER_DB = _env_float("JARVIS_ADAPTIVE_INPUT_CREST_DB", 22.0, 15.0, 45.0)

#: Consecutive degraded utterances before acting. One is a cough or a chair.
DEGRADED_RUN = _env_int("JARVIS_ADAPTIVE_INPUT_RUN", 3, 1)

#: Seconds of captured audio used to score a challenger.
PROBE_S = _env_float("JARVIS_ADAPTIVE_INPUT_PROBE_S", 1.5, 0.3, 5.0)

#: Minimum seconds between rebinds. A rebind costs an utterance; oscillating
#: between two mediocre microphones costs all of them.
COOLDOWN_S = _env_float("JARVIS_ADAPTIVE_INPUT_COOLDOWN_S", 120.0, 5.0, 3600.0)

#: A capture gap longer than this after a rebind means the new device is not
#: delivering — Bluetooth dropout, Continuity handoff, packet starvation.
STARVATION_MS = _env_float("JARVIS_ADAPTIVE_INPUT_STARVATION_MS", 100.0, 20.0, 5000.0)

#: How long to watch a freshly bound device before trusting it.
PROBATION_S = _env_float("JARVIS_ADAPTIVE_INPUT_PROBATION_S", 8.0, 1.0, 120.0)

#: Consecutive starvation events before a device is benched for the session.
STARVE_STRIKES = _env_int("JARVIS_ADAPTIVE_INPUT_STARVE_STRIKES", 2, 1)

#: Probe capture rate. The internal pipeline rate, so a probe measures what
#: the recogniser would be handed rather than something resampled afterwards.
_PROBE_RATE = 16000


@dataclass
class _Incumbent:
    """What we know about the device currently bound."""
    index: Optional[int] = None
    samples: List[QualitySample] = field(default_factory=list)

    def sqi(self) -> float:
        if not self.samples:
            return 0.0
        return float(np.median([s.sqi for s in self.samples]))

    def representative(self) -> Optional[QualitySample]:
        """The median-quality sample actually measured on this device.

        A REAL measurement rather than a synthesized one: ``sqi`` is a derived
        property, so a fabricated sample carrying a desired score would be a
        number with no audio behind it — the exact thing this investigation
        spent two sessions removing from the forensics."""
        if not self.samples:
            return None
        ordered = sorted(self.samples, key=lambda s: s.sqi)
        return ordered[len(ordered) // 2]


class AdaptiveInputManager:
    """Watches capture quality; rebinds to a closer microphone when one wins.

    Every collaborator is injected so the whole state machine is testable
    without CoreAudio: *rebind* performs the swap, *probe_factory* captures
    audio from a candidate index, *sd* is the sounddevice module.
    """

    def __init__(
        self,
        *,
        rebind: Callable[[Optional[int]], Any],
        probe_factory: Optional[Callable[[int, float], np.ndarray]] = None,
        sd: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rebind = rebind
        self._probe_factory = probe_factory
        self._sd = sd
        self._clock = clock

        self._incumbent = _Incumbent()
        self._builtin_index: Optional[int] = None
        self._degraded_run = 0
        self._armed = False
        self._last_rebind_at = -1e9
        self._busy = False

        #: Devices that starved us. Never offered again this session.
        self._benched: Dict[int, int] = {}
        self._probation_until = 0.0
        self._last_frame_at = 0.0
        self._starve_events = 0

        self.rebinds = 0
        self.fallbacks = 0

    # -- telemetry in ----------------------------------------------------

    def note_builtin(self, index: Optional[int]) -> None:
        """Record the device to fall back TO. Set once, at boot."""
        self._builtin_index = index
        if self._incumbent.index is None:
            self._incumbent.index = index

    def observe(self, sample: QualitySample) -> None:
        """One rejection's measurements from the live pipeline. NEVER raises."""
        try:
            self._incumbent.samples.append(sample)
            del self._incumbent.samples[:-8]
            if sample.crest_db > CREST_TRIGGER_DB:
                self._degraded_run += 1
            else:
                self._degraded_run = 0
            if self._degraded_run >= DEGRADED_RUN and not self._armed:
                self._armed = True
                logger.info(
                    "[AdaptiveInput] armed — %d consecutive utterances above "
                    "%.0fdB crest (latest %.1fdB). Will score other inputs on "
                    "the next speech.",
                    self._degraded_run, CREST_TRIGGER_DB, sample.crest_db,
                )
        except Exception:  # noqa: BLE001
            pass

    def note_capture_frame(self) -> None:
        """Called from the capture path. Feeds the starvation watchdog, and is
        deliberately trivial — it runs on the audio thread."""
        self._last_frame_at = self._clock()

    @property
    def armed(self) -> bool:
        return self._armed

    # -- the decision ----------------------------------------------------

    async def on_speech(self) -> bool:
        """Speech is happening NOW — the only honest moment to compare mics.

        Returns True when a rebind occurred. NEVER raises."""
        if not (self._armed and adaptive_input_enabled()):
            return False
        if self._busy:
            return False
        if self._clock() - self._last_rebind_at < COOLDOWN_S:
            return False
        self._busy = True
        try:
            return await self._evaluate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AdaptiveInput] evaluation degraded: %r", exc)
            return False
        finally:
            self._busy = False

    async def _evaluate(self) -> bool:
        incumbent_idx = self._incumbent.index
        incumbent_sample = self._incumbent.representative()

        def _probe(index: int) -> QualitySample:
            # The incumbent is scored from what the pipeline already measured.
            # Opening a second stream on the device in use is the contention
            # that hangs CoreAudio.
            if index == incumbent_idx:
                if incumbent_sample is None:
                    raise RuntimeError("no measurements for the incumbent yet")
                return incumbent_sample
            if index in self._benched:
                raise RuntimeError("benched this session")
            audio = self._capture(index, PROBE_S)
            return QualitySample.from_audio(audio, _PROBE_RATE)

        scores = await asyncio.to_thread(
            lambda: rank_devices(probe=_probe, sd=self._sd),
        )
        for s in scores:
            logger.info(
                "[AdaptiveInput] candidate %d %r sqi=%.3f%s%s",
                s.index, s.name, s.sqi,
                " continuity" if s.is_continuity else "",
                f" error={s.error}" if s.error else "",
            )

        winner = best_device(scores)
        if winner is None or winner.index == incumbent_idx:
            logger.info(
                "[AdaptiveInput] staying on %s — nothing cleared the margin",
                incumbent_idx,
            )
            self._armed = False
            self._degraded_run = 0
            return False

        return await self._swap_to(winner)

    async def _swap_to(self, winner: DeviceScore) -> bool:
        logger.info(
            "[AdaptiveInput] rebinding %s → %d %r (sqi %.3f)",
            self._incumbent.index, winner.index, winner.name, winner.sqi,
        )
        ok = await self._call_rebind(winner.index)
        if not ok:
            self._bench(winner.index, "rebind failed")
            self._armed = False
            return False

        self._incumbent = _Incumbent(index=winner.index)
        self._last_rebind_at = self._clock()
        self._probation_until = self._last_rebind_at + PROBATION_S
        self._last_frame_at = self._clock()
        self._armed = False
        self._degraded_run = 0
        self.rebinds += 1
        return True

    # -- the circuit breaker ---------------------------------------------

    async def check_liveness(self) -> bool:
        """Poll for capture starvation on a freshly bound device.

        A Continuity mic that hands off to the phone, an AirPod that drops, a
        BT link that stalls — all present the same way: the callback simply
        stops. Returns True when a fallback was performed. NEVER raises."""
        try:
            if not adaptive_input_enabled():
                return False
            if self._incumbent.index == self._builtin_index:
                return False
            now = self._clock()
            if now > self._probation_until:
                return False
            gap_ms = (now - self._last_frame_at) * 1000.0
            if gap_ms < STARVATION_MS:
                return False

            self._starve_events += 1
            logger.warning(
                "[AdaptiveInput] capture gap %.0fms on device %s (strike %d/%d)",
                gap_ms, self._incumbent.index, self._starve_events,
                STARVE_STRIKES,
            )
            if self._starve_events < STARVE_STRIKES:
                self._last_frame_at = now      # one more window to recover
                return False
            return await self.fall_back("capture starvation")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AdaptiveInput] liveness check degraded: %r", exc)
            return False

    async def fall_back(self, reason: str) -> bool:
        """Return to the built-in array. The one path that must always work.

        Benches the offending device so the manager cannot immediately choose
        it again and oscillate. NEVER raises."""
        failed = self._incumbent.index
        if failed is not None and failed != self._builtin_index:
            self._bench(failed, reason)
        logger.warning(
            "[AdaptiveInput] falling back %s → %s (%s)",
            failed, self._builtin_index, reason,
        )
        try:
            await self._call_rebind(self._builtin_index)
        except Exception as exc:  # noqa: BLE001
            logger.error("[AdaptiveInput] fallback rebind raised: %r", exc)
        # Incumbent is updated regardless. If even the built-in refused to
        # start, the bus has stopped itself and the truthful record is that we
        # are no longer on the remote device.
        self._incumbent = _Incumbent(index=self._builtin_index)
        self._starve_events = 0
        self._probation_until = 0.0
        self._armed = False
        self.fallbacks += 1
        return True

    # -- plumbing --------------------------------------------------------

    def _bench(self, index: int, reason: str) -> None:
        self._benched[index] = self._benched.get(index, 0) + 1
        logger.info("[AdaptiveInput] benched device %d (%s)", index, reason)

    async def _call_rebind(self, index: Optional[int]) -> bool:
        result = self._rebind(index)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        return bool(result)

    def _capture(self, index: int, seconds: float) -> np.ndarray:
        """Bounded capture from a candidate. Blocking — always run in a thread."""
        if self._probe_factory is not None:
            return self._probe_factory(index, seconds)
        sd = self._sd
        if sd is None:
            import sounddevice as sd  # type: ignore[no-redef]
        rate = _PROBE_RATE
        frames = int(rate * seconds)
        buf = sd.rec(
            frames, samplerate=rate, channels=1, dtype="float32", device=index,
        )
        sd.wait()
        return np.asarray(buf, dtype=np.float32).reshape(-1)

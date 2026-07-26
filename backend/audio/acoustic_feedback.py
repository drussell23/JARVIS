"""The seam between a rejected capture and Karen saying she cannot hear.

Kept separate from :mod:`acoustic_quality` so the metric definitions and the
policy stay independently testable, and separate from ``streaming_stt`` so the
STT never imports a voice path — that direction would close a loop between the
recogniser and the speaker, which is how a system ends up transcribing itself.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

_CONTROLLER: Optional[Any] = None


def _controller() -> Any:
    """Lazily built, with the emit and speak seams bound to the real bus and
    the real fast path — both looked up late so an audio host without either
    still measures, and only the SPEAKING degrades."""
    global _CONTROLLER
    if _CONTROLLER is not None:
        return _CONTROLLER
    from backend.audio.acoustic_quality import AcousticFeedbackController

    def _emit(kind: str, payload: dict) -> None:
        # DRY: the audio-state UDS server is the existing event bus. No new
        # transport, no second socket.
        try:
            from backend.audio.audio_state_ipc import broadcast
            broadcast(kind, payload)
        except (ImportError, AttributeError, OSError):
            pass

    def _speak(line: str) -> None:
        # Routed through the SAME zero-cost path the phatic acknowledgements
        # use: this is Karen reporting her own limitation, not a model turn,
        # and it must never cost a token or wait on a provider.
        try:
            from backend.audio.conversation_pipeline import speak_immediate
            speak_immediate(line)
        except (ImportError, AttributeError):
            logger.info("[Acoustic] (unspoken) %s", line)

    _CONTROLLER = AcousticFeedbackController(emit=_emit, speak=_speak)
    return _CONTROLLER


def report_rejection(audio: Any, sample_rate: int, peak: float, rms: float,
                     no_speech_prob: float = 0.0, device: str = "") -> Optional[dict]:
    """Turn one empty transcript into a measurement. NEVER raises."""
    try:
        from backend.audio.acoustic_quality import QualitySample
        from backend.audio.capture_forensics import _Ring

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if not x.size:
            return None
        # DRY: the ring's stats are the forensics' own formulas, so the numbers
        # here and in an incident file cannot disagree.
        ring = _Ring(sample_rate, max(1.0, x.size / max(sample_rate, 1)))
        ring.push(x)
        st = ring.stats()
        sample = QualitySample(
            modulation=float(st.get("syllabic_modulation_2_8hz", 0.0)),
            crest_db=float(st.get("crest_db", 0.0)),
            rms=float(rms), peak=float(peak),
            no_speech_prob=float(no_speech_prob), device=str(device),
        )
        return _controller().observe(sample)
    except (ImportError, AttributeError, TypeError, ValueError):
        logger.debug("[Acoustic] report degraded", exc_info=True)
        return None


def reset() -> None:
    """Test seam."""
    global _CONTROLLER
    _CONTROLLER = None


__all__ = ["report_rejection", "reset"]

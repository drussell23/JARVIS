from __future__ import annotations
import logging
from typing import Optional
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandPayload,
    get_default_voice_sensor,
)
from .classifier import HeuristicClassifier, VoiceIntent

logger = logging.getLogger("Ouroboros.Karen.VoiceBuild")

class VoiceBuildBridge:
    def __init__(self, voice_sensor=None, classifier=None, repo: str = "jarvis") -> None:
        # voice_sensor is an OPTIONAL explicit override. Left as None (the
        # default — and what the audio bootstrap now passes), the sensor is
        # resolved lazily per-call via get_default_voice_sensor(). This
        # avoids binding to None forever if the bridge is constructed
        # before IntakeLayerService publishes the sensor.
        self._sensor = voice_sensor
        self._classifier = classifier or HeuristicClassifier()
        self._repo = repo
    async def on_final_transcript(self, text: str, confidence: float = 1.0) -> Optional[str]:
        try:
            sensor = self._sensor or get_default_voice_sensor()
            if sensor is None:
                return None
            if self._classifier.classify(text) != VoiceIntent.BUILD:
                return None
            payload = VoiceCommandPayload(
                description=text, target_files=[], repo=self._repo,
                stt_confidence=confidence,
            )
            return await sensor.handle_voice_command(payload)
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceBuild] route failed", exc_info=True)
            return None

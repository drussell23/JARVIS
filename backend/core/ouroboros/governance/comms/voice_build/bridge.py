from __future__ import annotations
import logging
from typing import Optional
from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
    VoiceCommandPayload,
)
from .classifier import HeuristicClassifier, VoiceIntent

logger = logging.getLogger("Ouroboros.Karen.VoiceBuild")

class VoiceBuildBridge:
    def __init__(self, voice_sensor, classifier=None, repo: str = "jarvis") -> None:
        self._sensor = voice_sensor
        self._classifier = classifier or HeuristicClassifier()
        self._repo = repo
    async def on_final_transcript(self, text: str, confidence: float = 1.0) -> Optional[str]:
        try:
            if self._sensor is None:
                return None
            if self._classifier.classify(text) != VoiceIntent.BUILD:
                return None
            payload = VoiceCommandPayload(
                description=text, target_files=[], repo=self._repo,
                stt_confidence=confidence,
            )
            return await self._sensor.handle_voice_command(payload)
        except Exception:  # noqa: BLE001
            logger.debug("[VoiceBuild] route failed", exc_info=True)
            return None

"""
JARVIS Voice Bridge — Wires ConversationManager + BargeIn into the live pipeline.

This is the integration glue between:
  - STT pipeline (StreamingSTTEngine → transcripts)
  - ConversationManager (utterance classification + routing)
  - BargeInDetector (interrupt JARVIS mid-speech)
  - safe_say() (existing TTS function)
  - VoiceCommandSensor (code tasks → Ouroboros pipeline)
  - All 7 JARVIS tiers (personality, emergency, predictions, etc.)

Called from audio_pipeline_bootstrap.py during system startup.
Registers as a transcript hook so ALL voice input flows through
the ConversationManager before reaching any other handler.

Boundary Principle:
  Deterministic: Hook registration, module construction, routing.
  Agentic: Response content for complex queries (via ConversationManager).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Coroutine, Dict, Optional

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get(
    "JARVIS_VOICE_CONVERSATION_ENABLED", "true"
).lower() in ("true", "1", "yes")


class JarvisVoiceBridge:
    """Bridges ConversationManager + BargeIn into the live voice pipeline.

    Constructed during audio_pipeline_bootstrap() and registered as a
    transcript hook on RealTimeVoiceCommunicator. All transcribed speech
    flows through this bridge before reaching legacy handlers.

    Wiring:
      STT transcript → JarvisVoiceBridge.on_transcript()
        → ConversationManager.handle_utterance()
          → classify + route + respond
            → safe_say() for TTS (with barge-in monitoring)
            → VoiceCommandSensor for code tasks (→ Ouroboros)
    """

    def __init__(
        self,
        say_fn: Optional[Callable[..., Coroutine]] = None,
        voice_command_sensor: Optional[Any] = None,
        personality_engine: Optional[Any] = None,
        emergency_engine: Optional[Any] = None,
        predictive_engine: Optional[Any] = None,
        judgment_framework: Optional[Any] = None,
    ) -> None:
        self._say_fn = say_fn
        self._voice_sensor = voice_command_sensor
        self._conversation_mgr = None
        self._barge_in = None

        if not _ENABLED:
            logger.info("[VoiceBridge] Disabled (JARVIS_VOICE_CONVERSATION_ENABLED=false)")
            return

        # Construct ConversationManager
        try:
            from backend.voice.conversation_manager import ConversationManager
            self._conversation_mgr = ConversationManager(
                say_fn=say_fn,
                pipeline_fn=self._route_code_task,
                personality_engine=personality_engine,
                emergency_engine=emergency_engine,
                predictive_engine=predictive_engine,
                judgment_framework=judgment_framework,
            )
            logger.info("[VoiceBridge] ConversationManager constructed")
        except Exception as exc:
            logger.warning("[VoiceBridge] ConversationManager failed: %s", exc)

        # Construct BargeInDetector
        try:
            from backend.voice.barge_in_detector import BargeInDetector
            self._barge_in = BargeInDetector(
                on_barge_in=self._on_barge_in,
            )
            logger.info("[VoiceBridge] BargeInDetector constructed")
        except Exception as exc:
            logger.warning("[VoiceBridge] BargeInDetector failed: %s", exc)

    async def on_transcript(self, text: str, confidence: float = 1.0) -> Optional[str]:
        """Transcript hook — called for every transcribed utterance.

        Registered via RealTimeVoiceCommunicator.register_transcript_hook().
        Returns the response text or None if not handled.
        """
        if not _ENABLED or not self._conversation_mgr:
            return None

        if not text or not text.strip():
            return None

        try:
            response = await self._conversation_mgr.handle_utterance(
                text, stt_confidence=confidence,
            )
            return response
        except Exception as exc:
            logger.warning("[VoiceBridge] handle_utterance failed: %s", exc)
            return None

    async def _route_code_task(self, text: str) -> Optional[Dict[str, Any]]:
        """Route a code task to Ouroboros via VoiceCommandSensor.

        Called by ConversationManager when utterance_type == CODE_TASK.
        Creates a VoiceCommandPayload and submits to the sensor.
        """
        if not self._voice_sensor:
            logger.debug("[VoiceBridge] No VoiceCommandSensor — cannot route code task")
            return None

        try:
            from backend.core.ouroboros.governance.intake.sensors.voice_command_sensor import (
                VoiceCommandPayload,
            )
            payload = VoiceCommandPayload(
                description=text,
                target_files=[],  # Let the pipeline infer targets
                repo="jarvis",
                stt_confidence=1.0,
                evidence={"source": "conversation_manager"},
            )
            result = await self._voice_sensor.handle_voice_command(payload)
            return {"success": result == "enqueued", "result": result}
        except Exception as exc:
            logger.warning("[VoiceBridge] Code task routing failed: %s", exc)
            return {"success": False, "result": str(exc)}

    async def _on_barge_in(self, signal: str) -> None:
        """Called when Derek interrupts JARVIS mid-speech.

        The barge-in detector killed the TTS process. Now we need to
        re-enable listening and process whatever Derek said.
        """
        logger.info("[VoiceBridge] Barge-in detected — JARVIS interrupted")
        # The STT pipeline will automatically pick up the next utterance
        # since the speech gate was released by the barge-in detector.
        # No explicit action needed here — the next transcript will flow
        # through on_transcript() normally.

    async def inject_proactive(self, text: str) -> None:
        """Inject a proactive utterance (JARVIS speaks first).

        Called by PredictiveEngine, EmergencyProtocols, or pipeline
        completion handlers.
        """
        if self._conversation_mgr:
            await self._conversation_mgr.inject_proactive(text)

    def get_status(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {"enabled": _ENABLED}
        if self._conversation_mgr:
            status["conversation"] = self._conversation_mgr.get_status()
        if self._barge_in:
            status["barge_in"] = self._barge_in.get_status()
        return status


# ═══════════════════════════════════════════════════════════════════════════
# Factory function for audio_pipeline_bootstrap.py
# ═══════════════════════════════════════════════════════════════════════════

_global_bridge: Optional[JarvisVoiceBridge] = None


def get_voice_bridge() -> Optional[JarvisVoiceBridge]:
    """Get the global voice bridge instance."""
    return _global_bridge


def create_voice_bridge(
    say_fn: Optional[Callable[..., Coroutine]] = None,
    voice_command_sensor: Optional[Any] = None,
    personality_engine: Optional[Any] = None,
    emergency_engine: Optional[Any] = None,
    predictive_engine: Optional[Any] = None,
    judgment_framework: Optional[Any] = None,
) -> JarvisVoiceBridge:
    """Create and register the global voice bridge.

    Called from audio_pipeline_bootstrap.py or unified_supervisor.py
    during system startup.
    """
    global _global_bridge
    _global_bridge = JarvisVoiceBridge(
        say_fn=say_fn,
        voice_command_sensor=voice_command_sensor,
        personality_engine=personality_engine,
        emergency_engine=emergency_engine,
        predictive_engine=predictive_engine,
        judgment_framework=judgment_framework,
    )
    return _global_bridge


def wire_to_voice_communicator(
    communicator: Any,
    bridge: Optional[JarvisVoiceBridge] = None,
) -> bool:
    """Register the voice bridge as a transcript hook.

    Calling this means ALL transcribed speech flows through the
    ConversationManager before reaching any other handler.

    Args:
        communicator: RealTimeVoiceCommunicator instance
        bridge: JarvisVoiceBridge (uses global if None)

    Returns True if wired successfully.
    """
    bridge = bridge or _global_bridge
    if bridge is None:
        logger.debug("[VoiceBridge] No bridge to wire")
        return False

    if not hasattr(communicator, "register_transcript_hook"):
        logger.warning("[VoiceBridge] Communicator has no register_transcript_hook")
        return False

    try:
        communicator.register_transcript_hook(bridge.on_transcript)
        logger.info("[VoiceBridge] Wired to RealTimeVoiceCommunicator transcript hook")
        return True
    except Exception as exc:
        logger.warning("[VoiceBridge] Wiring failed: %s", exc)
        return False

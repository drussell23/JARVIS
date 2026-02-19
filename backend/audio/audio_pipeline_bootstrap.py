"""
Audio Pipeline Bootstrap (Composition Layer)
=============================================

Two-phase factory that creates, wires, and manages the lifecycle of all
real-time voice conversation components.

Phase 1 (early): AudioBus — called before narrator, fast (~1s)
Phase 2 (late):  ConversationPipeline — called after Intelligence, heavier (~5s)

Usage in supervisor:
    # Phase 1 (early startup)
    audio_bus = await audio_pipeline_bootstrap.start_audio_bus()

    # Phase 2 (after Intelligence phase provides LLM client)
    handle = await audio_pipeline_bootstrap.wire_conversation_pipeline(
        audio_bus=audio_bus,
        llm_client=model_serving,
        speech_state=speech_state_manager,
    )

    # Shutdown
    await audio_pipeline_bootstrap.shutdown(handle)
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PipelineHandle:
    """Lifecycle handle returned by wire_conversation_pipeline()."""
    audio_bus: object = None
    streaming_stt: object = None
    turn_detector: object = None
    barge_in: object = None
    tts_engine: object = None
    conversation_pipeline: object = None
    mode_dispatcher: object = None
    health_task: Optional[asyncio.Task] = None
    _bargein_vad_consumer: object = None  # stored for unregister on shutdown

    def get_status(self) -> dict:
        """Aggregate status from all components."""
        status = {}
        for name in [
            "audio_bus", "streaming_stt", "turn_detector",
            "barge_in", "conversation_pipeline", "mode_dispatcher",
        ]:
            comp = getattr(self, name, None)
            if comp is not None and hasattr(comp, "get_status"):
                try:
                    status[name] = comp.get_status()
                except Exception as e:
                    status[name] = {"error": str(e)}
            else:
                status[name] = None
        return status


async def start_audio_bus(timeout: float = 5.0):
    """
    Phase 1: Start AudioBus. Called early, before narrator.

    Creates AudioBus singleton + FullDuplexDevice + AEC.
    Returns the AudioBus instance (or None on failure).
    """
    try:
        from backend.audio.audio_bus import AudioBus
        bus = AudioBus.get_instance()
        # Shield to prevent singleton half-init on timeout (see MEMORY.md)
        await asyncio.wait_for(asyncio.shield(bus.start()), timeout=timeout)
        logger.info("[Bootstrap] AudioBus started (Phase 1)")
        return bus
    except asyncio.TimeoutError:
        logger.warning(f"[Bootstrap] AudioBus start timed out ({timeout}s)")
        return None
    except Exception as e:
        logger.warning(f"[Bootstrap] AudioBus start failed: {e}")
        return None


async def wire_conversation_pipeline(
    audio_bus,
    llm_client=None,
    speech_state=None,
    stt_timeout: float = 10.0,
    tts_timeout: float = 15.0,
) -> PipelineHandle:
    """
    Phase 2: Wire all conversation pipeline components.

    Called after Intelligence phase so llm_client (UnifiedModelServing) is
    available. Each sub-component is independently optional — partial
    wiring is OK (degraded mode).

    Returns a PipelineHandle for lifecycle management.
    """
    handle = PipelineHandle(audio_bus=audio_bus)
    loop = asyncio.get_running_loop()

    # 1. TTS singleton
    try:
        from backend.voice.engines.unified_tts_engine import get_tts_engine
        handle.tts_engine = await asyncio.wait_for(
            get_tts_engine(), timeout=tts_timeout,
        )
        logger.info("[Bootstrap] TTS singleton ready")
    except Exception as e:
        logger.warning(f"[Bootstrap] TTS init skipped: {e}")

    # 2. StreamingSTT — register as AudioBus mic consumer
    try:
        from backend.voice.streaming_stt import StreamingSTTEngine
        handle.streaming_stt = StreamingSTTEngine()
        await asyncio.wait_for(handle.streaming_stt.start(), timeout=stt_timeout)

        if audio_bus is not None:
            audio_bus.register_mic_consumer(handle.streaming_stt.on_audio_frame)
            logger.info("[Bootstrap] StreamingSTT registered on AudioBus")
        else:
            logger.info("[Bootstrap] StreamingSTT started (no AudioBus)")
    except Exception as e:
        logger.warning(f"[Bootstrap] StreamingSTT init skipped: {e}")
        handle.streaming_stt = None

    # 3. TurnDetector + BargeInController
    try:
        from backend.audio.turn_detector import TurnDetector
        from backend.audio.barge_in_controller import BargeInController

        handle.turn_detector = TurnDetector()

        handle.barge_in = BargeInController()
        handle.barge_in.set_loop(loop)

        if audio_bus is not None:
            handle.barge_in.set_audio_bus(audio_bus)
            # Register barge-in VAD callback as AudioBus mic consumer.
            handle._bargein_vad_consumer = _create_bargein_vad_consumer(handle.barge_in)
            audio_bus.register_mic_consumer(handle._bargein_vad_consumer)
            logger.info("[Bootstrap] BargeInController registered on AudioBus")

        if speech_state is not None:
            handle.barge_in.set_speech_state(speech_state)

    except Exception as e:
        logger.warning(f"[Bootstrap] TurnDetector/BargeIn skipped: {e}")

    # 4. ConversationPipeline
    try:
        from backend.audio.conversation_pipeline import ConversationPipeline

        handle.conversation_pipeline = ConversationPipeline(
            audio_bus=audio_bus,
            streaming_stt=handle.streaming_stt,
            turn_detector=handle.turn_detector,
            barge_in=handle.barge_in,
            tts_engine=handle.tts_engine,
            llm_client=llm_client,
        )
        logger.info("[Bootstrap] ConversationPipeline created")
    except Exception as e:
        logger.warning(f"[Bootstrap] ConversationPipeline init skipped: {e}")

    # 5. ModeDispatcher
    try:
        from backend.audio.mode_dispatcher import ModeDispatcher

        handle.mode_dispatcher = ModeDispatcher(
            conversation_pipeline=handle.conversation_pipeline,
            speech_state=speech_state,
        )
        # Wire AudioBus and TTS for biometric mode + speaker verification
        if audio_bus is not None:
            handle.mode_dispatcher.set_audio_bus(audio_bus)
        if handle.tts_engine is not None:
            handle.mode_dispatcher.set_tts_engine(handle.tts_engine)
        await handle.mode_dispatcher.start()
        logger.info("[Bootstrap] ModeDispatcher started")
    except Exception as e:
        logger.warning(f"[Bootstrap] ModeDispatcher init skipped: {e}")

    # 6. Register ModeDispatcher transcript hook on voice communicator
    try:
        from backend.agi_os.realtime_voice_communicator import (
            get_voice_communicator,
        )
        _communicator = await get_voice_communicator()
        if _communicator is not None and handle.mode_dispatcher is not None:
            if hasattr(_communicator, "register_transcript_hook"):
                _communicator.register_transcript_hook(
                    handle.mode_dispatcher.handle_transcript
                )
                logger.info("[Bootstrap] ModeDispatcher registered as transcript hook")
            else:
                logger.debug("[Bootstrap] Voice communicator lacks register_transcript_hook")
    except Exception as e:
        logger.debug(f"[Bootstrap] Transcript hook registration skipped: {e}")

    return handle


def _create_bargein_vad_consumer(barge_in):
    """
    Create a mic consumer callback that runs energy-based VAD and feeds
    results to the BargeInController.

    Runs in the audio thread — must be fast and non-blocking.
    """
    _energy_threshold = float(os.getenv("JARVIS_BARGEIN_ENERGY_THRESHOLD", "0.01"))

    def _on_frame(frame: np.ndarray) -> None:
        if frame.size == 0:
            return
        energy = float(np.sqrt(np.mean(frame ** 2)))
        is_speech = energy > _energy_threshold
        barge_in.on_vad_speech_detected(is_speech)

    return _on_frame


async def shutdown(handle: PipelineHandle) -> None:
    """
    Shutdown all components in reverse order.
    Each step has a timeout to prevent shutdown stall.
    """
    _timeout = 5.0

    # 1. ModeDispatcher
    if handle.mode_dispatcher is not None:
        try:
            await asyncio.wait_for(handle.mode_dispatcher.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] ModeDispatcher stop error: {e}")

    # 1b. Stop speaker verification (unregisters its AudioBus consumer)
    if handle.mode_dispatcher is not None:
        try:
            if hasattr(handle.mode_dispatcher, '_stop_speaker_verification'):
                await asyncio.wait_for(
                    handle.mode_dispatcher._stop_speaker_verification(),
                    timeout=_timeout,
                )
        except Exception:
            pass

    # 2. BargeInController — disable before pipeline teardown to prevent
    #    call_soon_threadsafe on a closing event loop from the audio thread.
    if handle.barge_in is not None:
        try:
            handle.barge_in.enabled = False
            handle.barge_in.set_loop(None)
        except Exception:
            pass

    # 2b. Unregister barge-in VAD consumer from AudioBus
    if handle._bargein_vad_consumer is not None and handle.audio_bus is not None:
        try:
            handle.audio_bus.unregister_mic_consumer(handle._bargein_vad_consumer)
        except Exception:
            pass

    # 3. ConversationPipeline
    if handle.conversation_pipeline is not None:
        try:
            await asyncio.wait_for(
                handle.conversation_pipeline.end_session(), timeout=_timeout,
            )
        except Exception as e:
            logger.debug(f"[Bootstrap] ConversationPipeline end error: {e}")

    # 3. StreamingSTT — unregister from AudioBus first
    if handle.streaming_stt is not None:
        if handle.audio_bus is not None:
            try:
                handle.audio_bus.unregister_mic_consumer(
                    handle.streaming_stt.on_audio_frame
                )
            except Exception:
                pass
        try:
            await asyncio.wait_for(handle.streaming_stt.stop(), timeout=_timeout)
        except Exception as e:
            logger.debug(f"[Bootstrap] StreamingSTT stop error: {e}")

    # 4. Cancel health task
    if handle.health_task is not None:
        handle.health_task.cancel()
        try:
            await handle.health_task
        except (asyncio.CancelledError, Exception):
            pass

    # Note: AudioBus is NOT stopped here — it has its own lifecycle
    # managed by the supervisor (started in Phase 1, stopped at shutdown).

    logger.info("[Bootstrap] Audio pipeline shutdown complete")

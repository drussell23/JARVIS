"""
Mode Dispatcher (Layer 6a)
===========================

Routes audio between conversation, command, and biometric modes.
Wake word detection runs in parallel with all modes.

Modes:
    COMMAND:       Existing wake-word → command → response cycle
    CONVERSATION:  Full-duplex real-time dialogue (new)
    BIOMETRIC:     Voice unlock authentication

Triggers:
    "JARVIS, let's chat" / "conversation mode"  → CONVERSATION
    "JARVIS, unlock my screen"                   → BIOMETRIC (temporary)
    "JARVIS, stop" / "goodbye" / 5min silence    → COMMAND
"""

import asyncio
import logging
import os
import queue
import time
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Inactivity timeout for conversation mode (seconds)
_INACTIVITY_TIMEOUT = float(os.getenv("JARVIS_CONV_INACTIVITY_TIMEOUT", "300"))


class VoiceMode(Enum):
    COMMAND = "command"            # Existing: wake word → command → response
    CONVERSATION = "conversation"  # New: full-duplex real-time dialogue
    BIOMETRIC = "biometric"        # Existing: voice unlock


# Phrases that trigger mode switches (lowercased)
_CONVERSATION_TRIGGERS = [
    "let's chat", "lets chat", "conversation mode",
    "talk to me", "let's talk", "lets talk",
    "voice conversation", "start conversation",
]

_EXIT_TRIGGERS = [
    "stop", "goodbye", "good bye", "bye",
    "exit conversation", "end conversation",
    "command mode", "that's all", "i'm done",
]

_BIOMETRIC_TRIGGERS = [
    "unlock my screen", "unlock screen",
    "voice unlock", "authenticate",
]


class ModeDispatcher:
    """
    Routes audio to the appropriate pipeline based on current mode.
    Wake word detection runs in parallel with all modes.
    """

    def __init__(
        self,
        conversation_pipeline=None,
        speech_state=None,
    ):
        self._current_mode = VoiceMode.COMMAND
        self._previous_mode: Optional[VoiceMode] = None
        self._last_activity = time.time()
        self._mode_change_callbacks = []

        # Pipeline references (set via constructor or setters)
        self._conversation_pipeline = conversation_pipeline
        self._speech_state = speech_state

        # Conversation task management
        self._conversation_task: Optional[asyncio.Task] = None

        # Stats
        self._mode_switches = 0
        self._mode_history = []

        # Biometric authentication state
        self._biometric_task: Optional[asyncio.Task] = None
        self._biometric_audio_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._biometric_audio_consumer: Optional[Callable] = None

        # Lazy-loaded service references (set by supervisor after two-tier init)
        self._audio_bus = None
        self._tts_engine = None
        self._voice_unlock_service = None
        self._vbia_adapter = None

    @property
    def current_mode(self) -> VoiceMode:
        return self._current_mode

    def set_conversation_pipeline(self, pipeline) -> None:
        """Set the ConversationPipeline reference."""
        self._conversation_pipeline = pipeline

    def set_speech_state(self, speech_state) -> None:
        """Set the UnifiedSpeechStateManager reference."""
        self._speech_state = speech_state

    def set_audio_bus(self, audio_bus) -> None:
        """Set the AudioBus reference for mic consumer registration."""
        self._audio_bus = audio_bus

    def set_tts_engine(self, tts_engine) -> None:
        """Set the TTS engine reference for biometric challenges."""
        self._tts_engine = tts_engine

    def set_voice_unlock_service(self, service) -> None:
        """Set the IntelligentVoiceUnlockService reference."""
        self._voice_unlock_service = service

    def set_vbia_adapter(self, adapter) -> None:
        """Set the TieredVBIAAdapter reference."""
        self._vbia_adapter = adapter

    def on_mode_change(self, callback) -> None:
        """Register a callback for mode changes: callback(old_mode, new_mode)."""
        self._mode_change_callbacks.append(callback)

    async def switch_mode(self, mode: VoiceMode) -> None:
        """
        Switch to a new voice mode.

        Handles transitions:
            COMMAND → CONVERSATION: enable conversation mode, start session
            CONVERSATION → COMMAND: disable conversation mode, end session
            * → BIOMETRIC: save previous mode, switch to biometric
            BIOMETRIC → *: restore previous mode
        """
        if mode == self._current_mode:
            return

        old_mode = self._current_mode
        logger.info(f"[ModeDispatcher] {old_mode.value} → {mode.value}")

        # Leave old mode
        await self._leave_mode(old_mode)

        # Enter new mode
        self._previous_mode = old_mode
        self._current_mode = mode
        self._last_activity = time.time()
        self._mode_switches += 1
        self._mode_history.append({
            "from": old_mode.value,
            "to": mode.value,
            "timestamp": time.time(),
        })

        await self._enter_mode(mode)

        # Notify callbacks
        for cb in self._mode_change_callbacks:
            try:
                result = cb(old_mode, mode)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.debug(f"[ModeDispatcher] Callback error: {e}")

    async def _leave_mode(self, mode: VoiceMode) -> None:
        """Clean up when leaving a mode."""
        if mode == VoiceMode.CONVERSATION:
            # Cancel conversation loop task
            if self._conversation_task is not None:
                self._conversation_task.cancel()
                try:
                    await self._conversation_task
                except asyncio.CancelledError:
                    pass
                self._conversation_task = None

            # Disable conversation mode in speech state
            if self._speech_state is not None:
                self._speech_state.set_conversation_mode(False)

            # End conversation session
            if self._conversation_pipeline is not None:
                await self._conversation_pipeline.end_session()

        elif mode == VoiceMode.BIOMETRIC:
            # Cancel biometric task if still running
            if self._biometric_task is not None:
                self._biometric_task.cancel()
                try:
                    await self._biometric_task
                except asyncio.CancelledError:
                    pass
                self._biometric_task = None

            # Unregister AudioBus mic consumer
            self._unregister_biometric_consumer()

            # Drain audio queue
            while not self._biometric_audio_queue.empty():
                try:
                    self._biometric_audio_queue.get_nowait()
                except queue.Empty:
                    break

            # Resume conversation only if it was the previous mode (was paused)
            if (
                self._previous_mode == VoiceMode.CONVERSATION
                and self._conversation_pipeline is not None
                and hasattr(self._conversation_pipeline, 'resume')
            ):
                await self._conversation_pipeline.resume()

    async def _enter_mode(self, mode: VoiceMode) -> None:
        """Set up when entering a mode."""
        if mode == VoiceMode.CONVERSATION:
            # Enable conversation mode (skip cooldown, AEC handles echo)
            if self._speech_state is not None:
                self._speech_state.set_conversation_mode(True)

            # Start conversation session AND run the loop
            if self._conversation_pipeline is not None:
                await self._conversation_pipeline.start_session()
                # Launch conversation loop as a background task
                self._conversation_task = asyncio.ensure_future(
                    self._conversation_pipeline.run()
                )
                self._conversation_task.add_done_callback(
                    self._on_conversation_done
                )

        elif mode == VoiceMode.BIOMETRIC:
            # Pause conversation if it was running
            if self._conversation_pipeline is not None and hasattr(
                self._conversation_pipeline, 'pause'
            ):
                await self._conversation_pipeline.pause()

            # Launch biometric authentication as background task
            self._biometric_task = asyncio.ensure_future(
                self._run_biometric_authentication()
            )
            self._biometric_task.add_done_callback(
                self._on_biometric_done
            )

    async def handle_transcript(self, text: str) -> Optional[VoiceMode]:
        """
        Check if a transcript should trigger a mode switch.

        Returns the new mode if a switch was triggered, None otherwise.
        Called by the wake word / command processor with transcribed text.
        """
        text_lower = text.lower().strip()
        self._last_activity = time.time()

        # Check for conversation triggers
        if self._current_mode == VoiceMode.COMMAND:
            for trigger in _CONVERSATION_TRIGGERS:
                if trigger in text_lower:
                    await self.switch_mode(VoiceMode.CONVERSATION)
                    return VoiceMode.CONVERSATION

        # Check for biometric triggers (from any mode)
        for trigger in _BIOMETRIC_TRIGGERS:
            if trigger in text_lower:
                await self.switch_mode(VoiceMode.BIOMETRIC)
                return VoiceMode.BIOMETRIC

        # Check for exit triggers (conversation → command)
        if self._current_mode == VoiceMode.CONVERSATION:
            for trigger in _EXIT_TRIGGERS:
                if trigger in text_lower:
                    await self.switch_mode(VoiceMode.COMMAND)
                    return VoiceMode.COMMAND

        return None

    async def return_from_biometric(self) -> None:
        """
        Return to previous mode after biometric authentication completes.
        """
        if self._current_mode == VoiceMode.BIOMETRIC:
            target = self._previous_mode or VoiceMode.COMMAND
            await self.switch_mode(target)

    async def check_inactivity(self) -> bool:
        """
        Check for conversation inactivity timeout.
        Returns True if mode was switched due to timeout.
        """
        if self._current_mode != VoiceMode.CONVERSATION:
            return False

        elapsed = time.time() - self._last_activity
        if elapsed > _INACTIVITY_TIMEOUT:
            logger.info(
                f"[ModeDispatcher] Conversation timed out "
                f"({elapsed:.0f}s > {_INACTIVITY_TIMEOUT}s)"
            )
            await self.switch_mode(VoiceMode.COMMAND)
            return True

        return False

    def get_status(self) -> dict:
        """Get dispatcher status."""
        return {
            "current_mode": self._current_mode.value,
            "previous_mode": (
                self._previous_mode.value if self._previous_mode else None
            ),
            "mode_switches": self._mode_switches,
            "last_activity": self._last_activity,
            "inactivity_seconds": time.time() - self._last_activity,
            "conversation_active": self._current_mode == VoiceMode.CONVERSATION,
            "biometric_active": self._current_mode == VoiceMode.BIOMETRIC,
            "biometric_task_running": (
                self._biometric_task is not None
                and not self._biometric_task.done()
            ),
        }

    def _on_conversation_done(self, task: asyncio.Task) -> None:
        """Handle conversation task completion (natural exit or error)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"[ModeDispatcher] Conversation task failed: {exc}")
        # Natural completion (e.g., user said "goodbye") — reset to command mode
        if self._current_mode == VoiceMode.CONVERSATION:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.switch_mode(VoiceMode.COMMAND))
            except RuntimeError:
                # No running loop (shutdown) — just update state directly
                self._current_mode = VoiceMode.COMMAND

    async def _run_biometric_authentication(self) -> None:
        """
        Execute voice biometric authentication flow.

        1. Register as AudioBus mic consumer to capture AEC-cleaned audio
        2. Speak challenge prompt via TTS->AudioBus (AEC reference)
        3. Capture user's voice response from AudioBus frames
        4. Run verification through IntelligentVoiceUnlockService
        5. Speak result via TTS->AudioBus
        6. Return to previous mode
        """
        import numpy as np

        try:
            # 1. Register mic consumer on AudioBus for AEC-cleaned capture
            # Drain any stale frames from previous attempt
            while not self._biometric_audio_queue.empty():
                try:
                    self._biometric_audio_queue.get_nowait()
                except queue.Empty:
                    break

            def _on_biometric_frame(frame: np.ndarray) -> None:
                """Accumulate AEC-cleaned audio frames (thread-safe via SimpleQueue)."""
                if frame.size > 0:
                    self._biometric_audio_queue.put_nowait(frame.copy())

            self._biometric_audio_consumer = _on_biometric_frame
            if self._audio_bus is not None:
                self._audio_bus.register_mic_consumer(_on_biometric_frame)

            # 2. Speak challenge via TTS->AudioBus for AEC reference
            await self._speak_biometric("Verifying your voice now.")

            # 3. Capture audio for verification (2.5 seconds of AEC-cleaned audio)
            capture_duration = float(
                os.getenv("JARVIS_BIOMETRIC_CAPTURE_DURATION", "2.5")
            )
            await asyncio.sleep(capture_duration)

            # 4. Drain queue and concatenate frames (thread-safe snapshot)
            frames: List[Any] = []
            while not self._biometric_audio_queue.empty():
                try:
                    frames.append(self._biometric_audio_queue.get_nowait())
                except queue.Empty:
                    break

            if not frames:
                await self._speak_biometric(
                    "I couldn't capture your voice. Please try again."
                )
                return

            audio_data = np.concatenate(frames)

            # Unregister consumer before processing (stop capturing)
            self._unregister_biometric_consumer()

            # 5. Run authentication
            auth_result = await self._authenticate_voice(audio_data)

            # 6. Speak result via TTS->AudioBus
            if auth_result.get("success"):
                speaker = auth_result.get("speaker", "")
                await self._speak_biometric(
                    f"Voice verified. Welcome back, {speaker}." if speaker
                    else "Voice verified. Unlocking now."
                )
            else:
                reason = auth_result.get("reason", "verification failed")
                await self._speak_biometric(
                    f"Voice verification unsuccessful. {reason}"
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[ModeDispatcher] Biometric auth error: {e}")
            try:
                await asyncio.shield(self._speak_biometric(
                    "Voice authentication encountered an error. "
                    "Please try again or use password unlock."
                ))
            except asyncio.CancelledError:
                pass  # Cleanup happens in finally
        finally:
            self._unregister_biometric_consumer()
            # Drain remaining queue frames
            while not self._biometric_audio_queue.empty():
                try:
                    self._biometric_audio_queue.get_nowait()
                except queue.Empty:
                    break

    async def _authenticate_voice(self, audio_data) -> Dict[str, Any]:
        """
        Run voice authentication through IntelligentVoiceUnlockService.

        Falls back to VBIA adapter if the unlock service is unavailable.
        """
        # Primary: IntelligentVoiceUnlockService
        if self._voice_unlock_service is not None:
            try:
                result = await asyncio.wait_for(
                    self._voice_unlock_service.process_voice_unlock_command(
                        audio_data=audio_data,
                        context={
                            "source": "mode_dispatcher",
                            "previous_mode": (
                                self._previous_mode.value
                                if self._previous_mode else "unknown"
                            ),
                            "aec_cleaned": True,
                        },
                    ),
                    timeout=float(os.getenv("JARVIS_BIOMETRIC_AUTH_TIMEOUT", "25")),
                )
                return result
            except asyncio.TimeoutError:
                logger.warning("[ModeDispatcher] Voice unlock timed out")
                return {"success": False, "reason": "Authentication timed out"}
            except Exception as e:
                logger.error(f"[ModeDispatcher] Voice unlock error: {e}")
                # Fall through to VBIA fallback intentionally

        # Fallback: TieredVBIAAdapter
        if self._vbia_adapter is not None:
            try:
                threshold = float(os.getenv("JARVIS_BIOMETRIC_THRESHOLD", "0.85"))
                passed, confidence = await self._vbia_adapter.verify_speaker(threshold)
                return {
                    "success": passed,
                    "confidence": confidence,
                    "reason": "verified" if passed else "below threshold",
                }
            except Exception as e:
                logger.error(f"[ModeDispatcher] VBIA fallback error: {e}")

        return {"success": False, "reason": "No authentication service available"}

    async def _speak_biometric(self, text: str) -> None:
        """
        Speak biometric feedback through TTS->AudioBus for AEC reference.

        Falls back to direct TTS if AudioBus is unavailable.
        """
        if self._tts_engine is None:
            logger.info(f"[ModeDispatcher] Biometric (no TTS): {text}")
            return

        try:
            import io
            import numpy as np

            if (
                hasattr(self._tts_engine, 'synthesize')
                and self._audio_bus is not None
            ):
                tts_result = await self._tts_engine.synthesize(text)
                if tts_result is not None:
                    audio_bytes = getattr(tts_result, 'audio_data', None)
                    sample_rate = getattr(tts_result, 'sample_rate', 22050)

                    if audio_bytes is not None:
                        try:
                            import soundfile as sf
                            audio_np, file_sr = sf.read(
                                io.BytesIO(audio_bytes), dtype='float32',
                            )
                            sample_rate = file_sr
                        except Exception as sf_err:
                            logger.debug(f"[ModeDispatcher] soundfile decode failed: {sf_err}, trying raw PCM")
                            audio_np = np.frombuffer(
                                audio_bytes, dtype=np.int16,
                            ).astype(np.float32) / 32767.0

                        _chunk_size = int(sample_rate * 0.1)

                        async def _chunks():
                            for i in range(0, len(audio_np), _chunk_size):
                                yield audio_np[i:i + _chunk_size]

                        await self._audio_bus.play_stream(
                            _chunks(), sample_rate,
                        )
                        return

            # Fallback: direct TTS (no AEC reference)
            if hasattr(self._tts_engine, 'speak'):
                await self._tts_engine.speak(text, play_audio=True, source="biometric")
        except Exception as e:
            logger.debug(f"[ModeDispatcher] Biometric TTS error: {e}")

    def _unregister_biometric_consumer(self) -> None:
        """Unregister biometric mic consumer from AudioBus."""
        if self._biometric_audio_consumer is not None and self._audio_bus is not None:
            try:
                self._audio_bus.unregister_mic_consumer(
                    self._biometric_audio_consumer
                )
            except Exception:
                pass
            self._biometric_audio_consumer = None

    def _on_biometric_done(self, task: asyncio.Task) -> None:
        """Handle biometric task completion - return to previous mode."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"[ModeDispatcher] Biometric task failed: {exc}")
        # Return to previous mode
        if self._current_mode == VoiceMode.BIOMETRIC:
            try:
                loop = asyncio.get_running_loop()
                t = loop.create_task(
                    self.return_from_biometric(), name="biometric-return"
                )
                t.add_done_callback(
                    lambda _t: _t.exception() if not _t.cancelled() else None
                )
            except RuntimeError:
                self._current_mode = self._previous_mode or VoiceMode.COMMAND

    async def start(self) -> None:
        """Start the mode dispatcher. Currently a no-op — modes are event-driven."""
        logger.info("[ModeDispatcher] Started")

    async def stop(self) -> None:
        """Stop the mode dispatcher and clean up any active mode."""
        if self._current_mode != VoiceMode.COMMAND:
            await self.switch_mode(VoiceMode.COMMAND)
        logger.info("[ModeDispatcher] Stopped")

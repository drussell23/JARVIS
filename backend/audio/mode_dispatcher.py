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
import time
from enum import Enum
from typing import Optional

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

    @property
    def current_mode(self) -> VoiceMode:
        return self._current_mode

    def set_conversation_pipeline(self, pipeline) -> None:
        """Set the ConversationPipeline reference."""
        self._conversation_pipeline = pipeline

    def set_speech_state(self, speech_state) -> None:
        """Set the UnifiedSpeechStateManager reference."""
        self._speech_state = speech_state

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
            pass  # Biometric mode handled by existing voice_unlock system

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
        }

    def _on_conversation_done(self, task: asyncio.Task) -> None:
        """Handle unexpected conversation task completion."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(f"[ModeDispatcher] Conversation task failed: {exc}")

    async def start(self) -> None:
        """Start the mode dispatcher. Currently a no-op — modes are event-driven."""
        logger.info("[ModeDispatcher] Started")

    async def stop(self) -> None:
        """Stop the mode dispatcher and clean up any active mode."""
        if self._current_mode != VoiceMode.COMMAND:
            await self.switch_mode(VoiceMode.COMMAND)
        logger.info("[ModeDispatcher] Stopped")

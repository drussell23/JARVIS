#!/usr/bin/env python3
"""
Display Monitor Voice Handler
==============================

Voice integration wrapper for display monitoring system.
Bridges the display monitor with JARVIS voice system.

This module provides voice output capabilities for the display monitoring system,
integrating with JARVIS voice components while providing fallback to macOS say
command for immediate audio feedback when displays are detected.

Author: Derek Russell
Date: 2025-10-15
Version: 1.0
"""

import asyncio
import logging
import subprocess
from typing import Optional, Dict, Any, List
import os

logger = logging.getLogger(__name__)


class DisplayVoiceHandler:
    """
    Voice handler for display monitoring system.

    Integrates with JARVIS voice systems and provides fallback mechanisms
    for reliable voice output during display monitoring operations.

    Attributes:
        voice_engine: JARVIS voice engine instance for text-to-speech
        voice_integration: JARVIS voice integration handler instance
        voice_enabled: Whether voice output is enabled
        voice_rate: Speech rate multiplier (1.0 = normal speed)
        voice_name: macOS voice name for say command

    Example:
        >>> handler = DisplayVoiceHandler()
        >>> await handler.speak("Display detected")
        >>> handler.set_voice_rate(1.2)  # Slightly faster speech
    """

    def __init__(self, voice_engine: Optional[Any] = None, voice_integration: Optional[Any] = None) -> None:
        """
        Initialize voice handler with optional JARVIS voice components.

        Args:
            voice_engine: JARVIS voice engine instance for text-to-speech
            voice_integration: JARVIS voice integration handler instance

        Example:
            >>> handler = DisplayVoiceHandler()
            >>> # Or with voice engine
            >>> from engines.voice_engine import VoiceEngine
            >>> engine = VoiceEngine()
            >>> handler = DisplayVoiceHandler(voice_engine=engine)
        """
        self.voice_engine = voice_engine
        self.voice_integration = voice_integration

        # Voice settings from environment
        self.voice_enabled = os.getenv('JARVIS_VOICE_ENABLED', 'true').lower() == 'true'
        self.voice_rate = float(os.getenv('JARVIS_VOICE_RATE', '1.0'))
        self.voice_name = os.getenv('JARVIS_VOICE_NAME', 'Daniel')  # British male voice

        logger.info(f"[DISPLAY VOICE] Initialized (enabled={self.voice_enabled})")

    async def speak(self, message: str, priority: str = "normal") -> None:
        """
        Speak a message using available voice systems.

        Attempts to use JARVIS voice systems first, then falls back to macOS say
        command for immediate audio feedback. For display monitoring, prioritizes
        immediate response over queued notifications.

        Args:
            message: Text message to speak
            priority: Priority level for voice output ("low", "normal", "high", "urgent")

        Example:
            >>> await handler.speak("TV detected in living room")
            >>> await handler.speak("Critical display error", priority="urgent")
        """
        if not self.voice_enabled:
            logger.debug(f"[DISPLAY VOICE] Voice disabled, skipping: {message}")
            return

        logger.info(f"[DISPLAY VOICE] Speaking: {message}")

        # For display monitor, always use macOS say for immediate feedback
        # This ensures the announcement is heard right when the TV is detected
        await self._speak_with_say(message)

    async def speak_async(self, message: str, priority: str = "normal") -> None:
        """
        Alias for speak method - for backward compatibility with AdvancedDisplayMonitor.

        Args:
            message: Text message to speak
            priority: Priority level for voice output ("low", "normal", "high", "urgent")

        Example:
            >>> await handler.speak_async("Display configuration changed")
        """
        await self.speak(message, priority)

    async def _try_jarvis_voice(self, message: str, priority: str) -> bool:
        """
        Attempt to use JARVIS voice systems for text-to-speech.

        Tries voice_engine first if available. Skips voice_integration to avoid
        queued notifications and ensure immediate audio feedback for display events.

        Args:
            message: Text message to speak
            priority: Priority level for voice output

        Returns:
            True if JARVIS voice system was used successfully, False otherwise

        Raises:
            Exception: Logs but doesn't raise exceptions from voice engine failures
        """
        # For display monitor, we want immediate audio feedback
        # Skip voice_integration (which queues notifications) and use macOS say directly
        # This ensures users hear the announcement immediately when TV is detected

        # Try voice_engine if available
        if self.voice_engine:
            try:
                # Check if voice_engine has speak method
                if hasattr(self.voice_engine, 'speak'):
                    await self.voice_engine.speak(message)
                    logger.debug("[DISPLAY VOICE] Used voice_engine")
                    return True
                elif hasattr(self.voice_engine, 'text_to_speech'):
                    await self.voice_engine.text_to_speech(message)
                    logger.debug("[DISPLAY VOICE] Used voice_engine.text_to_speech")
                    return True
            except Exception as e:
                logger.warning(f"[DISPLAY VOICE] voice_engine error: {e}")

        return False

    async def _speak_with_say(self, message: str) -> None:
        """
        Speak using macOS say command as fallback mechanism.

        Executes the macOS say command with configured voice settings.
        Runs asynchronously without blocking to provide immediate audio feedback.

        Args:
            message: Text message to speak

        Raises:
            FileNotFoundError: When say command is not available (non-macOS systems)
            Exception: Other subprocess execution errors (logged but not raised)

        Example:
            >>> await handler._speak_with_say("Hello world")
        """
        try:
            # Build say command
            cmd = ['say']

            # Add voice name
            if self.voice_name:
                cmd.extend(['-v', self.voice_name])

            # Add rate
            if self.voice_rate != 1.0:
                rate = int(175 * self.voice_rate)  # 175 is default rate
                cmd.extend(['-r', str(rate)])

            # Add message
            cmd.append(message)

            # Run in background (non-blocking)
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            logger.debug(f"[DISPLAY VOICE] Used macOS say command (voice={self.voice_name}, rate={self.voice_rate})")

            # Don't wait for completion (fire and forget)
            # await process.wait()

        except FileNotFoundError:
            logger.error("[DISPLAY VOICE] say command not found (not on macOS?)")
        except Exception as e:
            logger.error(f"[DISPLAY VOICE] say command error: {e}")

    def set_enabled(self, enabled: bool) -> None:
        """
        Enable or disable voice output.

        Args:
            enabled: True to enable voice output, False to disable

        Example:
            >>> handler.set_enabled(False)  # Disable voice
            >>> handler.set_enabled(True)   # Re-enable voice
        """
        self.voice_enabled = enabled
        logger.info(f"[DISPLAY VOICE] Voice {'enabled' if enabled else 'disabled'}")

    def set_voice_name(self, voice_name: str) -> None:
        """
        Set the voice name for macOS say command.

        Args:
            voice_name: Name of macOS voice to use (e.g., "Daniel", "Samantha", "Alex")

        Example:
            >>> handler.set_voice_name("Samantha")  # Female voice
            >>> handler.set_voice_name("Daniel")    # British male voice
        """
        self.voice_name = voice_name
        logger.info(f"[DISPLAY VOICE] Voice name set to: {voice_name}")

    def set_voice_rate(self, rate: float) -> None:
        """
        Set the speech rate multiplier.

        Args:
            rate: Speech rate multiplier where 1.0 is normal speed,
                 0.5 is half speed, 2.0 is double speed

        Example:
            >>> handler.set_voice_rate(1.2)  # 20% faster
            >>> handler.set_voice_rate(0.8)  # 20% slower
        """
        self.voice_rate = rate
        logger.info(f"[DISPLAY VOICE] Voice rate set to: {rate}")

    def get_available_voices(self) -> List[str]:
        """
        Get list of available macOS voices.

        Returns:
            List of available voice names, empty list if unable to retrieve

        Raises:
            Exception: Subprocess errors (logged but not raised)

        Example:
            >>> voices = handler.get_available_voices()
            >>> print(voices[:5])  # Show first 5 voices
            ['Agnes', 'Albert', 'Alex', 'Alice', 'Alva']
        """
        try:
            result = subprocess.run(
                ['say', '-v', '?'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                voices = []
                for line in result.stdout.split('\n'):
                    if line.strip():
                        # Parse voice name (first word)
                        parts = line.split()
                        if parts:
                            voices.append(parts[0])
                return voices

            return []

        except Exception as e:
            logger.error(f"[DISPLAY VOICE] Error getting voices: {e}")
            return []

    async def test_voice(self, test_message: str = "JARVIS display monitoring is online, sir.") -> None:
        """
        Test voice output with a sample message.

        Args:
            test_message: Message to use for testing voice output

        Example:
            >>> await handler.test_voice()
            >>> await handler.test_voice("Custom test message")
        """
        logger.info("[DISPLAY VOICE] Testing voice...")
        await self.speak(test_message, priority="normal")


def create_voice_handler(voice_engine: Optional[Any] = None, voice_integration: Optional[Any] = None) -> DisplayVoiceHandler:
    """
    Create voice handler with automatic detection of JARVIS voice systems.

    Factory function that attempts to auto-detect and initialize JARVIS voice
    components if not explicitly provided. For display monitoring, skips
    voice_integration to ensure immediate audio feedback.

    Args:
        voice_engine: Optional voice engine instance, will attempt auto-detection if None
        voice_integration: Optional voice integration instance (ignored for display monitoring)

    Returns:
        Configured DisplayVoiceHandler instance

    Example:
        >>> handler = create_voice_handler()
        >>> # Or with explicit voice engine
        >>> from engines.voice_engine import VoiceEngine
        >>> engine = VoiceEngine()
        >>> handler = create_voice_handler(voice_engine=engine)
    """
    # Try to auto-detect if not provided
    if voice_engine is None:
        try:
            from engines.voice_engine import VoiceEngine
            voice_engine = VoiceEngine()
            logger.info("[DISPLAY VOICE] Auto-detected VoiceEngine")
        except Exception:
            pass

    # Don't auto-detect voice_integration for display monitor
    # We want immediate audio feedback via macOS say, not queued notifications
    # if voice_integration is None:
    #     try:
    #         from vision.voice_integration_handler import VoiceIntegrationHandler
    #         voice_integration = VoiceIntegrationHandler()
    #         logger.info("[DISPLAY VOICE] Auto-detected VoiceIntegrationHandler")
    #     except:
    #         pass

    return DisplayVoiceHandler(voice_engine, voice_integration=None)


if __name__ == "__main__":
    """
    Test script for DisplayVoiceHandler.
    
    Demonstrates voice handler functionality including:
    - Voice enumeration
    - Basic voice output
    - Custom message testing
    """
    # Test the voice handler
    async def test() -> None:
        """Test function for voice handler capabilities."""
        logging.basicConfig(level=logging.INFO)

        handler = create_voice_handler()

        print("Available voices:")
        voices = handler.get_available_voices()
        for voice in voices[:10]:  # Show first 10
            print(f"  - {voice}")

        print("\nTesting voice output...")
        await handler.test_voice()

        print("\nTesting with different message...")
        await handler.speak("Sir, your Living Room TV is now available. Would you like to extend your display?")

    asyncio.run(test())
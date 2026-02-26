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

try:
    from backend.core.supervisor.unified_voice_orchestrator import (
        get_voice_orchestrator,
        VoicePriority,
        VoiceSource,
        SpeechTopic,
    )
    VOICE_ORCHESTRATOR_AVAILABLE = True
except Exception:
    VOICE_ORCHESTRATOR_AVAILABLE = False
    get_voice_orchestrator = None
    VoicePriority = None
    VoiceSource = None
    SpeechTopic = None


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
        self._voice_orchestrator = get_voice_orchestrator() if VOICE_ORCHESTRATOR_AVAILABLE else None

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

        # Prefer unified orchestrator to prevent overlapping voice pipelines.
        if await self._speak_with_unified_orchestrator(message, priority):
            return

        # Fallback to direct macOS say when orchestrator path is unavailable.
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
            # v275.2: Safe say -o tempfile path to avoid device contention
            import tempfile as _tmpmod

            # Build say command with -o flag
            cmd = ['say']
            if self.voice_name:
                cmd.extend(['-v', self.voice_name])
            if self.voice_rate != 1.0:
                rate = int(175 * self.voice_rate)
                cmd.extend(['-r', str(rate)])

            _temp_fd, _temp_path = _tmpmod.mkstemp(
                suffix=".aiff", prefix="jarvis_display_"
            )
            import os as _os
            _os.close(_temp_fd)

            try:
                cmd.extend(['-o', _temp_path, message])

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL
                )

                try:
                    await asyncio.wait_for(process.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    process.kill()
                    logger.warning("[DISPLAY VOICE] say process timed out, killed")
                    return

                # Play safely via AudioBus or afplay
                _played = False
                try:
                    from backend.audio.audio_bus import AudioBus as _ABCls
                    _ab = _ABCls.get_instance_safe()
                    if _ab is not None and _ab.is_running:
                        import soundfile as _sf
                        import numpy as _np
                        _data, _sr = _sf.read(_temp_path, dtype="float32")
                        if isinstance(_data, _np.ndarray) and _data.ndim > 1:
                            _data = _np.mean(_data, axis=1, dtype=_np.float32)
                        _data = _np.asarray(_data, dtype=_np.float32).reshape(-1)
                        await _ab.play_audio(_data, _sr)
                        _played = True
                except Exception:
                    pass

                if not _played:
                    _play_proc = await asyncio.create_subprocess_exec(
                        "afplay", _temp_path,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(_play_proc.wait(), timeout=30.0)

                logger.debug(f"[DISPLAY VOICE] Used safe say -o (voice={self.voice_name}, rate={self.voice_rate})")
            finally:
                try:
                    _os.unlink(_temp_path)
                except OSError:
                    pass

        except FileNotFoundError:
            logger.error("[DISPLAY VOICE] say command not found (not on macOS?)")
        except Exception as e:
            logger.error(f"[DISPLAY VOICE] say command error: {e}")

    async def _speak_with_unified_orchestrator(self, message: str, priority: str) -> bool:
        """
        Route display narration through the unified orchestrator when available.

        This keeps display prompts in the same deduplicated queue as supervisor
        narration and avoids overlapping `say` subprocesses.
        """
        if not self._voice_orchestrator or not VOICE_ORCHESTRATOR_AVAILABLE:
            return False

        priority_map = {
            "low": VoicePriority.LOW,
            "normal": VoicePriority.MEDIUM,
            "high": VoicePriority.HIGH,
            "urgent": VoicePriority.CRITICAL,
        }
        mapped_priority = priority_map.get(priority.lower(), VoicePriority.MEDIUM)

        try:
            accepted = await self._voice_orchestrator.speak(
                text=message,
                priority=mapped_priority,
                source=VoiceSource.SYSTEM,
                wait=False,
                topic=SpeechTopic.GENERAL,
            )
            if accepted:
                logger.debug("[DISPLAY VOICE] Routed through unified voice orchestrator")
            return bool(accepted)
        except Exception as e:
            logger.debug(f"[DISPLAY VOICE] Unified orchestrator path failed: {e}")
            return False

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

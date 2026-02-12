"""
Voice Unlock Integration Service
==================================

Integrates the new SpeechBrain-based voice enrollment system with
the existing macOS screen unlock infrastructure.

This bridges:
- New: SpeechBrain speaker verification with Cloud SQL voiceprints
- Existing: Mac unlock automation, keychain, screen lock detection
"""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

# Add parent directories to path
sys.path.append(str(Path(__file__).parent.parent))

from intelligence.learning_database import JARVISLearningDatabase
from voice.speaker_verification_service import SpeakerVerificationService
from voice_unlock.services.keychain_service import KeychainService

# Fix for potential circular import or missing module in some environments
try:
    from voice_unlock.objc.server.screen_lock_detector import is_screen_locked
except ImportError:
    # Fallback implementation if module is missing
    def is_screen_locked() -> bool:
        """Fallback screen lock check using Quartz via ctypes or shell command."""
        try:
            import Quartz
            session_dict = Quartz.CGSessionCopyCurrentDictionary()
            return bool(session_dict and session_dict.get("CGSSessionScreenIsLocked", 0))
        except ImportError:
            # Shell fallback (slower but works without Quartz)
            import subprocess
            try:
                result = subprocess.run(
                    'python3 -c "import Quartz; print(Quartz.CGSessionCopyCurrentDictionary().get(\'CGSSessionScreenIsLocked\', 0))"',
                    shell=True, capture_output=True, text=True
                )
                return result.stdout.strip() == "1"
            except Exception:
                return False  # Assume unlocked on error to prevent lockout

logger = logging.getLogger(__name__)


class VoiceUnlockIntegration:
    """
    Integrated voice unlock service combining new voice system with existing unlock infrastructure

    Features:
    - Real speaker verification using SpeechBrain ECAPA-TDNN embeddings
    - Cloud SQL voiceprint storage
    - macOS screen lock detection
    - Keychain password retrieval
    - Learning database integration for audit trail
    """

    def __init__(self):
        self.speaker_verification = None
        self.keychain_service = None
        self.learning_db = None
        self._owns_learning_db = False
        self.initialized = False
        self.enable_voice_feedback = True  # Enable spoken responses

        # JARVIS Voice configuration (matching codebase standards)
        self.jarvis_voice_name = os.getenv('JARVIS_VOICE_NAME', 'Daniel')  # British male voice
        self.jarvis_voice_rate = int(os.getenv('JARVIS_VOICE_RATE_WPM', '175'))  # Words per minute
        self.urgent_voice_rate = 200  # For urgent/important messages

        # Configuration - these are now base thresholds, actual will be adaptive
        self.base_unlock_threshold = 0.40  # Base threshold, adaptive system will adjust (matches owner-aware fusion)
        self.base_general_threshold = 0.40  # For general speaker identification
        self.min_unlock_threshold = 0.25  # Minimum threshold (safety floor)
        self.use_adaptive_thresholds = True  # Enable adaptive threshold system

        # Statistics
        self.stats = {
            "total_unlock_attempts": 0,
            "successful_unlocks": 0,
            "failed_verifications": 0,
            "rejected_low_confidence": 0,
            "screen_already_unlocked": 0,
        }

    async def initialize(self):
        """Initialize all services"""
        if self.initialized:
            return

        logger.info("🚀 Initializing Voice Unlock Integration...")

        try:
            # Initialize speaker verification with Cloud SQL
            self.speaker_verification = SpeakerVerificationService()
            await self.speaker_verification.initialize()
            logger.info("✅ Speaker verification initialized")

            # Initialize keychain service
            self.keychain_service = KeychainService()
            logger.info("✅ Keychain service initialized")

            # Initialize learning database using singleton
            from intelligence.learning_database import get_learning_database
            self.learning_db = await get_learning_database()
            self._owns_learning_db = False
            logger.info("✅ Learning database initialized")

            self.initialized = True
            logger.info("✅ Voice Unlock Integration ready")

        except Exception as e:
            logger.error(f"Failed to initialize Voice Unlock Integration: {e}", exc_info=True)
            raise

    async def verify_and_unlock(
        self, audio_data: bytes, speaker_name: Optional[str] = None, command_text: Optional[str] = None
    ) -> Tuple[bool, str, float]:
        """
        Verify speaker and unlock screen if authorized

        Args:
            audio_data: Audio bytes from microphone
            speaker_name: Expected speaker name (if None, uses primary user from profiles)
            command_text: Optional command text that triggered unlock

        Returns:
            Tuple of (success, message, confidence)
        """
        if not self.initialized:
            await self.initialize()

        self.stats["total_unlock_attempts"] += 1

        try:
            # Step 1: Check if screen is already unlocked
            if not is_screen_locked():
                logger.info("📱 Screen is already unlocked")
                self.stats["screen_already_unlocked"] += 1
                return True, "Screen is already unlocked", 1.0

            # Step 2: Determine speaker name (use primary user if not specified)
            if not speaker_name:
                # Find primary user from speaker profiles
                for name, profile in self.speaker_verification.speaker_profiles.items():
                    if profile.get("is_primary_user", False):
                        speaker_name = name
                        logger.info(f"🔍 Using primary user for verification: {speaker_name}")
                        break

                if not speaker_name:
                    # No primary user found, try to identify from audio
                    logger.info("🔍 No speaker specified and no primary user - identifying from audio")
                    result = await self.speaker_verification.verify_speaker(audio_data, None)
                    speaker_name = result.get("speaker_name", "unknown")
                    if speaker_name == "unknown":
                        return False, "No voice profile found. Please enroll your voice first.", 0.0

            # Step 3: Verify speaker identity
            logger.info(f"🎤 Verifying speaker: {speaker_name}")
            result = await self.speaker_verification.verify_speaker(
                audio_data, speaker_name
            )

            # Extract values from result dict
            is_verified = result["verified"]
            confidence = result["confidence"]
            speaker_id = result.get("speaker_id")

            # Get adaptive threshold if available
            adaptive_threshold = result.get("adaptive_threshold", self.base_unlock_threshold)

            logger.info(
                f"🔐 Verification result: {speaker_name}, "
                f"Confidence: {confidence:.2%}, Verified: {is_verified}, "
                f"Threshold: {adaptive_threshold:.2%}"
            )

            # Step 3: Check confidence threshold for unlock
            unlock_threshold = adaptive_threshold if self.use_adaptive_thresholds else self.base_unlock_threshold

            if confidence < unlock_threshold:
                logger.warning(
                    f"❌ Confidence too low for unlock: {confidence:.2%} < {unlock_threshold:.2%}"
                )
                self.stats["rejected_low_confidence"] += 1

                # Record failed attempt
                await self._record_unlock_attempt(
                    speaker_name=speaker_name,
                    confidence=confidence,
                    success=False,
                    reason="Low confidence",
                    command_text=command_text,
                )

                # 🔊 SPEAK FAILURE MESSAGE
                await self._speak_jarvis("Voice not recognized", urgent=True)

                # Provide helpful message based on confidence level
                if confidence < 0.10:
                    suggestion = result.get("suggestion", "")
                    message = (
                        f"Voice verification failed (confidence: {confidence:.2%}). "
                        f"{suggestion}. Try speaking more clearly or re-enroll your voice profile."
                    )
                elif confidence < 0.30:
                    message = (
                        f"Voice verification failed (confidence: {confidence:.2%}). "
                        f"The system is learning your voice patterns. Please try again."
                    )
                else:
                    message = f"Voice verification failed (confidence: {confidence:.2%}). Please try again."

                return False, message, confidence

            if not is_verified:
                logger.warning(f"❌ Speaker not verified: {speaker_name}")
                self.stats["failed_verifications"] += 1

                # Record failed attempt
                await self._record_unlock_attempt(
                    speaker_name=speaker_name,
                    confidence=confidence,
                    success=False,
                    reason="Speaker not verified",
                    command_text=command_text,
                )

                # 🔊 SPEAK FAILURE MESSAGE
                await self._speak_jarvis("Access denied", urgent=True)

                return False, "Speaker verification failed", confidence

            # Step 4: Retrieve password from keychain
            logger.info("🔑 Retrieving unlock password from keychain...")
            password = self._get_unlock_password()

            if not password:
                logger.error("❌ Failed to retrieve unlock password")
                return False, "Failed to retrieve unlock password", confidence

            # Step 5: Execute unlock
            logger.info("🔓 Executing screen unlock...")
            unlock_success = await self._execute_unlock(password)

            if unlock_success:
                logger.info(f"✅ Screen unlocked successfully for {speaker_name}")
                self.stats["successful_unlocks"] += 1

                # Record successful unlock
                await self._record_unlock_attempt(
                    speaker_name=speaker_name,
                    confidence=confidence,
                    success=True,
                    reason="Successful unlock",
                    command_text=command_text,
                )

                # Silent unlock for speed - no audio feedback needed
                # User wants instant unlock without voice delay
                welcome_message = f"Welcome, {speaker_name}!"

                return True, f"Screen unlocked. {welcome_message}", confidence
            else:
                logger.error("❌ Unlock execution failed")
                # Speak failure message
                await self._speak_jarvis("Unlock failed", urgent=True)
                return False, "Unlock execution failed", confidence

        except Exception as e:
            logger.error(f"Error during verify and unlock: {e}", exc_info=True)
            # Speak error message
            await self._speak_jarvis("Authentication error", urgent=True)
            return False, f"Error: {str(e)}", 0.0

    def _get_unlock_password(self) -> Optional[str]:
        """Retrieve unlock password from keychain"""
        try:
            # Try to get password from keychain
            password = self.keychain_service.get_password(
                service="com.jarvis.voiceunlock", account="unlock_token"
            )

            if password:
                return password

            # Fallback: Try to get from environment variable (for testing)
            import os

            password = os.environ.get("JARVIS_UNLOCK_PASSWORD")

            if password:
                logger.warning(
                    "Using password from environment variable (not recommended for production)"
                )
                return password

            logger.error("No unlock password found in keychain or environment")
            return None

        except Exception as e:
            logger.error(f"Error retrieving unlock password: {e}", exc_info=True)
            return None

    async def _execute_unlock(self, password: str) -> bool:
        """Execute the actual screen unlock"""
        try:
            # Use AppleScript to type password and unlock
            unlock_script = f"""
            tell application "System Events"
                -- Wake display
                do shell script "caffeinate -u -t 1"
                delay 0.5

                -- Activate loginwindow if needed
                set frontApp to name of first process whose frontmost is true
                if frontApp contains "loginwindow" then
                    -- Type password
                    keystroke "{password}"
                    delay 0.2
                    keystroke return
                    delay 1.5
                end if
            end tell
            """

            import subprocess

            result = subprocess.run(
                ["osascript", "-e", unlock_script], capture_output=True, text=True, timeout=5
            )

            if result.returncode != 0:
                logger.error(f"Unlock script failed: {result.stderr}")
                return False

            # Verify unlock
            await asyncio.sleep(0.5)
            if not is_screen_locked():
                logger.info("✅ Unlock verified successful")
                return True
            else:
                logger.warning("⚠️ Screen still locked after unlock attempt")
                return False

        except Exception as e:
            logger.error(f"Error executing unlock: {e}", exc_info=True)
            return False

    async def _record_unlock_attempt(
        self,
        speaker_name: str,
        confidence: float,
        success: bool,
        reason: str,
        command_text: Optional[str] = None,
    ):
        """Record unlock attempt in learning database for audit trail"""
        try:
            if self.learning_db:
                # Record in conversation history
                await self.learning_db.record_interaction(
                    user_query=command_text or "unlock screen",
                    jarvis_response=reason,
                    response_type="voice_unlock",
                    confidence_score=confidence,
                    success=success,
                    metadata={
                        "speaker_name": speaker_name,
                        "unlock_attempt": True,
                        "reason": reason,
                    },
                )
        except Exception as e:
            logger.error(f"Error recording unlock attempt: {e}", exc_info=True)

    async def identify_speaker(self, audio_data: bytes) -> Tuple[Optional[str], float, bool]:
        """
        Identify speaker from audio without unlocking

        Args:
            audio_data: Audio bytes from microphone

        Returns:
            Tuple of (speaker_name, confidence, is_owner)
        """
        if not self.initialized:
            await self.initialize()

        try:
            # Use general threshold for identification
            result = await self.speaker_verification.identify_speaker(audio_data)

            if result:
                speaker_name = result.get("speaker_name")
                confidence = result.get("confidence", 0.0)
                is_owner = result.get("is_primary_user", False)

                return speaker_name, confidence, is_owner
            else:
                # GRACEFUL DEGRADATION: Return low confidence instead of hard fail
                logger.info("🔄 No speaker result - returning low confidence for fallback")
                return None, 0.10, False

        except Exception as e:
            logger.error(f"Error identifying speaker: {e}", exc_info=True)
            # GRACEFUL DEGRADATION: Return minimal confidence instead of hard fail
            # This allows upstream systems to attempt alternative authentication
            logger.info("🔄 Speaker identification error - enabling graceful degradation")
            return None, 0.05, False

    def get_stats(self) -> dict:
        """Get unlock statistics"""
        return {
            **self.stats,
            "success_rate": (
                self.stats["successful_unlocks"] / self.stats["total_unlock_attempts"]
                if self.stats["total_unlock_attempts"] > 0
                else 0.0
            ),
        }

    async def _speak_jarvis(self, message: str, urgent: bool = False) -> None:
        """
        Speak message using JARVIS voice (Daniel - British male voice)

        Uses macOS 'say' command with the same voice configuration used throughout the codebase.
        Runs asynchronously in the background to not block unlock operations.

        Args:
            message: Text to speak
            urgent: If True, uses faster speech rate for important messages
        """
        if not self.enable_voice_feedback:
            return

        try:
            # Use urgent rate for important messages, normal rate otherwise
            rate = self.urgent_voice_rate if urgent else self.jarvis_voice_rate

            # Run say command asynchronously in background (non-blocking)
            proc = await asyncio.create_subprocess_exec(
                'say',
                '-v', self.jarvis_voice_name,
                '-r', str(rate),
                message,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )

            # Don't wait for completion - let it speak in background
            logger.info(f"🔊 [JARVIS VOICE] Speaking: '{message}' (voice={self.jarvis_voice_name}, rate={rate} WPM)")

        except Exception as e:
            logger.error(f"Failed to speak with JARVIS voice: {e}")

    async def cleanup(self):
        """Cleanup resources"""
        if self.speaker_verification:
            await self.speaker_verification.cleanup()
        if self.learning_db:
            if self._owns_learning_db:
                await self.learning_db.close()
            self.learning_db = None
            self._owns_learning_db = False
        self.initialized = False
        logger.info("🧹 Voice Unlock Integration cleaned up")


# Singleton instance
_voice_unlock_integration = None


async def get_voice_unlock_integration() -> VoiceUnlockIntegration:
    """Get or create singleton instance"""
    global _voice_unlock_integration

    if _voice_unlock_integration is None:
        _voice_unlock_integration = VoiceUnlockIntegration()
        await _voice_unlock_integration.initialize()

    return _voice_unlock_integration

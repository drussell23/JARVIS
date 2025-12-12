"""
Speaker-Aware Command Handler for JARVIS
=========================================

Integrates voice biometric verification with every voice command to provide:
1. Automatic speaker identification (Derek J. Russell)
2. Personalized responses with user's name
3. Context-Aware Intelligence (CAI) - screen state, time, location
4. Scenario-Aware Intelligence (SAI) - routine vs emergency detection
5. Automatic screen unlock for verified owner
6. Security-enhanced command processing

Uses 25 voice samples stored in learning database for biometric matching.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class SpeakerContext:
    """Speaker identification context"""

    speaker_name: str
    confidence: float
    is_owner: bool
    security_level: str
    verified: bool
    verification_time: datetime


@dataclass
class CommandContext:
    """Full context for command processing"""

    speaker: Optional[SpeakerContext]
    screen_locked: bool
    screen_state: str  # "locked", "unlocked", "screensaver"
    time_of_day: str  # "morning", "afternoon", "evening", "night"
    location_category: str  # "home", "work", "other"
    is_routine_time: bool  # Based on user's typical patterns
    scenario_type: str  # "routine", "emergency", "suspicious"
    cai_confidence: float  # Context-Aware Intelligence confidence
    sai_confidence: float  # Scenario-Aware Intelligence confidence


class SpeakerAwareCommandHandler:
    """
    Handles voice commands with speaker verification and contextual intelligence

    Features:
    - Automatic speaker identification from voice
    - Personalized greetings using speaker's name
    - CAI: Screen state, time, location analysis
    - SAI: Routine/emergency/suspicious detection
    - Automatic unlock for verified owner + safe context
    - Security-enhanced command routing
    """

    def __init__(self):
        self.speaker_verification_service = None
        self.cai_system = None  # Context-Aware Intelligence
        self.sai_system = None  # Scenario-Aware Intelligence
        self.screen_controller = None
        self.initialized = False

        # Speaker verification threshold
        self.verification_threshold = 0.75  # 75% confidence required
        self.owner_unlock_threshold = 0.85  # 85% for automatic unlock

        # Statistics
        self.stats = {
            "total_commands": 0,
            "verified_commands": 0,
            "failed_verifications": 0,
            "auto_unlocks": 0,
            "security_blocks": 0,
        }

    async def initialize(self):
        """Initialize all subsystems"""
        if self.initialized:
            return

        logger.info("🎤 Initializing Speaker-Aware Command Handler...")

        try:
            # Initialize speaker verification service
            from voice.speaker_verification_service import get_speaker_verification_service

            self.speaker_verification_service = await get_speaker_verification_service()
            logger.info("✅ Speaker verification loaded (25 voice samples)")

        except Exception as e:
            logger.error(f"Failed to initialize speaker verification: {e}")

        try:
            # Initialize CAI (Context-Aware Intelligence)
            from context_intelligence.context_aware_core import ContextAwareCore

            self.cai_system = ContextAwareCore()
            await self.cai_system.initialize()
            logger.info("✅ Context-Aware Intelligence (CAI) loaded")

        except Exception as e:
            logger.warning(f"CAI not available: {e}")

        try:
            # Initialize SAI (Scenario-Aware Intelligence)
            from intelligence.learning_database import get_learning_database

            self.sai_database = await get_learning_database()
            logger.info("✅ Scenario-Aware Intelligence (SAI) loaded")

        except Exception as e:
            logger.warning(f"SAI not available: {e}")

        try:
            # Initialize screen controller for unlocking
            from system_control.macos_controller_v2 import MacOSControllerV2

            self.screen_controller = MacOSControllerV2()
            logger.info("✅ Screen controller loaded")

        except Exception as e:
            logger.warning(f"Screen controller not available: {e}")

        self.initialized = True
        logger.info("🎉 Speaker-Aware Command Handler ready!")

    async def process_voice_command(
        self, audio_data: bytes, transcription: str, command_handler: callable
    ) -> Dict[str, Any]:
        """
        Process voice command with speaker verification and contextual intelligence

        Args:
            audio_data: Raw audio bytes for speaker verification
            transcription: Transcribed text of the command
            command_handler: Function to handle the actual command

        Returns:
            Response dict with speaker info, context, and command result
        """
        if not self.initialized:
            await self.initialize()

        self.stats["total_commands"] += 1
        start_time = datetime.now()

        # Step 1: Verify speaker identity
        logger.info(f"🎤 Processing command: '{transcription}'")
        speaker_result = await self._verify_speaker(audio_data)

        if not speaker_result["verified"]:
            self.stats["failed_verifications"] += 1
            return await self._handle_unverified_speaker(transcription, speaker_result)

        self.stats["verified_commands"] += 1
        speaker_ctx = SpeakerContext(
            speaker_name=speaker_result["speaker_name"],
            confidence=speaker_result["confidence"],
            is_owner=speaker_result["is_owner"],
            security_level=speaker_result["security_level"],
            verified=True,
            verification_time=datetime.now(),
        )

        logger.info(
            f"✅ Speaker verified: {speaker_ctx.speaker_name} "
            f"(confidence: {speaker_ctx.confidence:.1%}, owner: {speaker_ctx.is_owner})"
        )

        # Step 2: Analyze context (CAI - Context-Aware Intelligence)
        context = await self._analyze_context(transcription, speaker_ctx)

        # Step 3: Analyze scenario (SAI - Scenario-Aware Intelligence)
        scenario = await self._analyze_scenario(transcription, speaker_ctx, context)
        context.scenario_type = scenario["type"]
        context.sai_confidence = scenario["confidence"]

        logger.info(
            f"📊 Context: screen={context.screen_state}, "
            f"time={context.time_of_day}, scenario={context.scenario_type}"
        )

        # Step 4: Check if automatic unlock is needed
        unlock_result = await self._handle_automatic_unlock(transcription, speaker_ctx, context)

        # Step 5: Create personalized greeting
        greeting = await self._create_personalized_greeting(speaker_ctx, context, unlock_result)

        # Step 6: Process the actual command
        try:
            command_result = await command_handler(transcription, context)
        except Exception as e:
            logger.error(f"Command processing error: {e}")
            command_result = {
                "success": False,
                "error": str(e),
                "response": f"Sorry {speaker_ctx.speaker_name}, I encountered an error.",
            }

        # Step 7: Build complete response
        response = {
            "success": True,
            "speaker": {
                "name": speaker_ctx.speaker_name,
                "verified": speaker_ctx.verified,
                "confidence": speaker_ctx.confidence,
                "is_owner": speaker_ctx.is_owner,
                "security_level": speaker_ctx.security_level,
            },
            "context": {
                "screen_locked": context.screen_locked,
                "screen_state": context.screen_state,
                "time_of_day": context.time_of_day,
                "location": context.location_category,
                "scenario": context.scenario_type,
                "cai_confidence": context.cai_confidence,
                "sai_confidence": context.sai_confidence,
            },
            "unlock": unlock_result,
            "greeting": greeting,
            "command_result": command_result,
            "processing_time_ms": (datetime.now() - start_time).total_seconds() * 1000,
        }

        # Add personalized greeting to response text
        if greeting and command_result.get("response"):
            response["response"] = f"{greeting} {command_result['response']}"
        elif greeting:
            response["response"] = greeting
        else:
            response["response"] = command_result.get("response", "")

        return response

    async def _verify_speaker(self, audio_data: bytes) -> Dict[str, Any]:
        """Verify speaker identity from audio"""
        if not self.speaker_verification_service:
            logger.warning("Speaker verification not available, assuming owner")
            return {
                "verified": True,
                "confidence": 0.5,
                "speaker_name": "Derek",
                "speaker_id": None,
                "is_owner": True,
                "security_level": "standard",
            }

        try:
            result = await self.speaker_verification_service.verify_speaker(audio_data)

            # Check if confidence meets threshold
            if result["confidence"] >= self.verification_threshold:
                result["verified"] = True
            else:
                result["verified"] = False
                logger.warning(
                    f"Low confidence verification: {result['confidence']:.1%} "
                    f"(threshold: {self.verification_threshold:.1%})"
                )

            return result

        except Exception as e:
            logger.error(f"Speaker verification error: {e}")
            return {
                "verified": False,
                "confidence": 0.0,
                "speaker_name": "unknown",
                "speaker_id": None,
                "is_owner": False,
                "security_level": "none",
                "error": str(e),
            }

    async def _analyze_context(
        self, transcription: str, speaker_ctx: SpeakerContext
    ) -> CommandContext:
        """Analyze context using CAI (Context-Aware Intelligence)"""

        # Get screen state
        screen_locked = await self._is_screen_locked()
        screen_state = "locked" if screen_locked else "unlocked"

        # Get time of day
        hour = datetime.now().hour
        if 5 <= hour < 12:
            time_of_day = "morning"
        elif 12 <= hour < 17:
            time_of_day = "afternoon"
        elif 17 <= hour < 21:
            time_of_day = "evening"
        else:
            time_of_day = "night"

        # Get location category (simplified - could use WiFi SSID, GPS, etc.)
        location_category = "home"  # Default assumption

        # Check if routine time
        is_routine_time = await self._is_routine_time(speaker_ctx.speaker_name, hour)

        # CAI confidence based on available information
        cai_confidence = 0.9 if self.cai_system else 0.7

        return CommandContext(
            speaker=speaker_ctx,
            screen_locked=screen_locked,
            screen_state=screen_state,
            time_of_day=time_of_day,
            location_category=location_category,
            is_routine_time=is_routine_time,
            scenario_type="unknown",  # Will be set by SAI
            cai_confidence=cai_confidence,
            sai_confidence=0.0,  # Will be set by SAI
        )

    async def _analyze_scenario(
        self, transcription: str, speaker_ctx: SpeakerContext, context: CommandContext
    ) -> Dict[str, Any]:
        """Analyze scenario using SAI (Scenario-Aware Intelligence)"""

        # Check for emergency keywords
        emergency_keywords = ["emergency", "urgent", "help", "alarm", "fire", "police", "ambulance"]
        is_emergency = any(kw in transcription.lower() for kw in emergency_keywords)

        # Check for suspicious indicators
        suspicious_indicators = []

        # Suspicious if unverified speaker + locked screen + sensitive command
        if not speaker_ctx.verified and context.screen_locked:
            suspicious_indicators.append("unverified_locked")

        # Suspicious if requesting unlock at unusual time
        if context.time_of_day == "night" and not context.is_routine_time:
            if "unlock" in transcription.lower():
                suspicious_indicators.append("unusual_time_unlock")

        # Determine scenario type
        if is_emergency:
            scenario_type = "emergency"
            confidence = 0.9
        elif suspicious_indicators:
            scenario_type = "suspicious"
            confidence = 0.8
        elif context.is_routine_time:
            scenario_type = "routine"
            confidence = 0.95
        else:
            scenario_type = "normal"
            confidence = 0.85

        return {
            "type": scenario_type,
            "confidence": confidence,
            "indicators": suspicious_indicators if suspicious_indicators else None,
            "is_emergency": is_emergency,
        }

    async def _handle_automatic_unlock(
        self, transcription: str, speaker_ctx: SpeakerContext, context: CommandContext
    ) -> Dict[str, Any]:
        """Handle automatic screen unlock for verified owner"""

        # Check if unlock is needed
        if not context.screen_locked:
            return {"attempted": False, "reason": "screen_already_unlocked"}

        # IMPORTANT: Never auto-unlock for explicit LOCK requests.
        # Token-based check avoids substring collisions ("unlock" contains "lock").
        try:
            import re

            tokens = set(re.findall(r"[a-z']+", (transcription or "").lower()))
            is_explicit_lock = ("lock" in tokens) and ("unlock" not in tokens)
            if is_explicit_lock:
                return {"attempted": False, "reason": "lock_command_no_unlock"}
        except Exception:
            # If tokenization fails, continue to next safety gate
            pass

        # Context-aware gating: only unlock when the *command* actually requires screen access.
        # This prevents accidental unlocks for voice-only commands while the screen is locked.
        try:
            from context_intelligence.detectors.screen_lock_detector import get_screen_lock_detector

            detector = get_screen_lock_detector()
            screen_ctx = await detector.check_screen_context(
                transcription, speaker_name=speaker_ctx.speaker_name
            )
            if not screen_ctx.get("requires_unlock"):
                return {"attempted": False, "reason": "command_does_not_require_unlock"}
        except Exception:
            # If context intelligence isn't available, fall back to existing behavior
            pass

        # Check if speaker is verified owner
        if not speaker_ctx.is_owner or not speaker_ctx.verified:
            return {
                "attempted": False,
                "reason": "not_owner",
                "security_note": "Only device owner can auto-unlock",
            }

        # Check confidence threshold for unlock
        if speaker_ctx.confidence < self.owner_unlock_threshold:
            logger.warning(
                f"Confidence too low for auto-unlock: {speaker_ctx.confidence:.1%} "
                f"(required: {self.owner_unlock_threshold:.1%})"
            )
            return {
                "attempted": False,
                "reason": "low_confidence",
                "confidence": speaker_ctx.confidence,
                "required": self.owner_unlock_threshold,
            }

        # Check for suspicious scenario
        if context.scenario_type == "suspicious":
            logger.warning("Blocking auto-unlock due to suspicious scenario")
            self.stats["security_blocks"] += 1
            return {
                "attempted": False,
                "reason": "suspicious_scenario",
                "security_note": "Manual unlock required for suspicious scenarios",
            }

        # All checks passed - attempt unlock
        logger.info(f"🔓 Attempting automatic unlock for {speaker_ctx.speaker_name}")

        try:
            if self.screen_controller:
                unlock_success = await self._unlock_screen()

                if unlock_success:
                    self.stats["auto_unlocks"] += 1
                    logger.info(f"✅ Screen automatically unlocked for {speaker_ctx.speaker_name}")
                    return {
                        "attempted": True,
                        "success": True,
                        "method": "voice_biometric",
                        "speaker": speaker_ctx.speaker_name,
                        "confidence": speaker_ctx.confidence,
                    }
                else:
                    logger.error("Screen unlock failed")
                    return {"attempted": True, "success": False, "error": "unlock_failed"}
            else:
                return {"attempted": False, "reason": "controller_not_available"}

        except Exception as e:
            logger.error(f"Auto-unlock error: {e}")
            return {"attempted": True, "success": False, "error": str(e)}

    async def _create_personalized_greeting(
        self, speaker_ctx: SpeakerContext, context: CommandContext, unlock_result: Dict[str, Any]
    ) -> str:
        """Create personalized greeting using speaker's name"""

        # Don't greet if speaker not verified
        if not speaker_ctx.verified:
            return ""

        name = speaker_ctx.speaker_name

        # Special greeting if we just unlocked the screen
        if unlock_result.get("success"):
            greetings = [
                f"Welcome back, {name}. I've unlocked your screen.",
                f"Screen unlocked, {name}. How can I help?",
                f"Hello {name}, screen is now unlocked.",
            ]
        # Time-based greetings
        elif context.time_of_day == "morning":
            greetings = [
                f"Good morning, {name}.",
                f"Morning, {name}.",
                f"Hello {name}, good morning.",
            ]
        elif context.time_of_day == "afternoon":
            greetings = [
                f"Good afternoon, {name}.",
                f"Afternoon, {name}.",
                f"Hello {name}.",
            ]
        elif context.time_of_day == "evening":
            greetings = [
                f"Good evening, {name}.",
                f"Evening, {name}.",
                f"Hello {name}.",
            ]
        else:  # night
            greetings = [
                f"Hello {name}.",
                f"Hi {name}.",
            ]

        # Return first greeting (could randomize)
        return greetings[0]

    async def _handle_unverified_speaker(
        self, transcription: str, speaker_result: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Handle commands from unverified speakers"""

        logger.warning(
            f"Unverified speaker attempted command: '{transcription}' "
            f"(confidence: {speaker_result.get('confidence', 0.0):.1%})"
        )

        return {
            "success": False,
            "verified": False,
            "speaker": speaker_result,
            "response": (
                "I couldn't verify your identity. For security-sensitive operations, "
                "please try again or use manual authentication."
            ),
            "security_note": "Voice verification failed",
        }

    async def _is_screen_locked(self) -> bool:
        """Check if screen is locked"""
        try:
            if self.screen_controller:
                return await self.screen_controller.is_screen_locked()
        except Exception as e:
            logger.debug(f"Could not check screen lock status: {e}")

        return False  # Assume unlocked if can't check

    async def _unlock_screen(self) -> bool:
        """Unlock the screen"""
        try:
            if self.screen_controller:
                result = await self.screen_controller.unlock_screen()
                return result.get("success", False)
        except Exception as e:
            logger.error(f"Screen unlock error: {e}")

        return False

    async def _is_routine_time(self, speaker_name: str, hour: int) -> bool:
        """Check if current time matches user's routine patterns"""
        try:
            if self.sai_database:
                # Query user's typical active hours from learning database
                # This would analyze historical command patterns
                # For now, simple heuristic: 7am-11pm is routine
                return 7 <= hour <= 23
        except Exception:
            pass

        return 7 <= hour <= 23  # Default assumption

    def get_stats(self) -> Dict[str, Any]:
        """Get handler statistics"""
        success_rate = (
            self.stats["verified_commands"] / self.stats["total_commands"] * 100
            if self.stats["total_commands"] > 0
            else 0
        )

        return {
            **self.stats,
            "verification_success_rate": round(success_rate, 1),
            "verification_threshold": self.verification_threshold,
            "owner_unlock_threshold": self.owner_unlock_threshold,
        }


# Global singleton instance
_speaker_aware_handler: Optional[SpeakerAwareCommandHandler] = None


async def get_speaker_aware_handler() -> SpeakerAwareCommandHandler:
    """Get global speaker-aware command handler instance"""
    global _speaker_aware_handler

    if _speaker_aware_handler is None:
        _speaker_aware_handler = SpeakerAwareCommandHandler()
        await _speaker_aware_handler.initialize()

    return _speaker_aware_handler

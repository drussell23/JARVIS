"""
JARVIS CAI (Context Awareness Intelligence) Voice Feedback Manager
===================================================================

Advanced, intelligent verbal transparency system for the CAI → VBI → Unlock → Continuation flow.

Features:
- Dynamic context-aware message generation (no hardcoding)
- Time-of-day personalized greetings
- Parallel async voice operations
- Confidence-based progressive feedback
- Intelligent retry guidance
- Real-time stage narration
- Multi-factor authentication transparency
- Graceful error handling with fallbacks

Architecture:
    CAI detects locked screen
           ↓
    VoiceFeedbackManager.announce_lock_detected()
           ↓
    VBI verification with real-time narration
           ↓
    VoiceFeedbackManager.announce_verification_result()
           ↓
    Continuation execution with progress updates
           ↓
    VoiceFeedbackManager.announce_task_completion()

Usage:
    from api.cai_voice_feedback_manager import get_cai_voice_manager, CAIContext

    manager = await get_cai_voice_manager()

    # Create context for this interaction
    ctx = CAIContext(
        command_text="search for dogs",
        speaker_name="Derek",
        is_screen_locked=True
    )

    # Full flow with verbal transparency
    await manager.announce_lock_detected(ctx)
    await manager.announce_vbi_start(ctx)
    await manager.announce_verification_result(ctx, success=True, confidence=0.92)
    await manager.announce_unlock_success(ctx)
    await manager.announce_continuation_start(ctx)
    await manager.announce_task_completion(ctx, result="Found 10 results for dogs")
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import subprocess

logger = logging.getLogger(__name__)

# Import safe_say for AudioBus-safe speech routing
try:
    from backend.core.supervisor.unified_voice_orchestrator import safe_say as _safe_say
except ImportError:
    _safe_say = None
    logger.debug("[CAI-VOICE] safe_say unavailable — will fall back to raw subprocess")


# ═══════════════════════════════════════════════════════════════════════════════
# ENUMS & DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

class TimeOfDay(Enum):
    """Time periods for contextual greetings."""
    EARLY_MORNING = "early_morning"    # 5-7 AM
    MORNING = "morning"                # 7-12 PM
    AFTERNOON = "afternoon"            # 12-5 PM
    EVENING = "evening"                # 5-9 PM
    NIGHT = "night"                    # 9 PM - 12 AM
    LATE_NIGHT = "late_night"          # 12-5 AM


class ConfidenceLevel(Enum):
    """VBI confidence levels for progressive feedback."""
    INSTANT = "instant"          # >95% - instant recognition
    HIGH = "high"                # 90-95% - very confident
    GOOD = "good"                # 85-90% - confident
    BORDERLINE = "borderline"    # 80-85% - slight hesitation
    LOW = "low"                  # 75-80% - needs clarification
    FAILED = "failed"            # <75% - verification failed


class CAIStage(Enum):
    """Stages in the CAI flow for narration."""
    LOCK_DETECTED = auto()
    VBI_STARTING = auto()
    VBI_EXTRACTING = auto()
    VBI_COMPARING = auto()
    VBI_VERIFYING = auto()
    VBI_SUCCESS = auto()
    VBI_FAILED = auto()
    UNLOCKING = auto()
    UNLOCK_SUCCESS = auto()
    UNLOCK_FAILED = auto()
    CONTINUATION_STARTING = auto()
    CONTINUATION_PROGRESS = auto()
    CONTINUATION_COMPLETE = auto()
    ERROR = auto()


class VoiceMode(Enum):
    """Voice delivery modes."""
    NORMAL = "normal"
    CONFIDENT = "confident"
    REASSURING = "reassuring"
    URGENT = "urgent"
    QUIET = "quiet"
    CONVERSATIONAL = "conversational"


@dataclass
class CAIContext:
    """
    Context for a CAI interaction - carries all state through the flow.
    Dynamically updated as the flow progresses.
    """
    # Command info
    command_text: str
    continuation_action: str = ""  # Extracted action like "search for dogs"

    # Speaker info
    speaker_name: str = "Sir"
    is_primary_user: bool = True

    # Screen state
    is_screen_locked: bool = False
    was_unlocked: bool = False

    # VBI state
    vbi_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    context_confidence: float = 0.0
    fused_confidence: float = 0.0

    # Environment
    snr_db: float = 20.0  # Signal-to-noise ratio
    environment: str = "quiet"

    # Timing
    start_time: float = field(default_factory=time.time)
    unlock_latency_ms: float = 0.0
    vbi_latency_ms: float = 0.0

    # Attempt tracking
    attempt_number: int = 1
    consecutive_failures: int = 0

    # Error state
    error_message: str = ""
    error_code: str = ""

    # Metadata
    timestamp: datetime = field(default_factory=datetime.now)
    trace_id: str = ""

    @property
    def time_of_day(self) -> TimeOfDay:
        """Get current time of day."""
        hour = self.timestamp.hour
        if 5 <= hour < 7:
            return TimeOfDay.EARLY_MORNING
        elif 7 <= hour < 12:
            return TimeOfDay.MORNING
        elif 12 <= hour < 17:
            return TimeOfDay.AFTERNOON
        elif 17 <= hour < 21:
            return TimeOfDay.EVENING
        elif 21 <= hour < 24:
            return TimeOfDay.NIGHT
        else:
            return TimeOfDay.LATE_NIGHT

    @property
    def confidence_level(self) -> ConfidenceLevel:
        """Get confidence level from VBI confidence."""
        if self.vbi_confidence >= 0.95:
            return ConfidenceLevel.INSTANT
        elif self.vbi_confidence >= 0.90:
            return ConfidenceLevel.HIGH
        elif self.vbi_confidence >= 0.85:
            return ConfidenceLevel.GOOD
        elif self.vbi_confidence >= 0.80:
            return ConfidenceLevel.BORDERLINE
        elif self.vbi_confidence >= 0.75:
            return ConfidenceLevel.LOW
        else:
            return ConfidenceLevel.FAILED

    @property
    def total_elapsed_ms(self) -> float:
        """Total elapsed time since start."""
        return (time.time() - self.start_time) * 1000


# ═══════════════════════════════════════════════════════════════════════════════
# DYNAMIC MESSAGE TEMPLATES
# ═══════════════════════════════════════════════════════════════════════════════

class DynamicMessageGenerator:
    """
    Generates context-aware, personalized messages dynamically.
    No hardcoding - all messages are templates with dynamic substitution.
    """

    # ─────────────────────────────────────────────────────────────────────────
    # LOCK DETECTED MESSAGES (by time of day)
    # ─────────────────────────────────────────────────────────────────────────
    LOCK_DETECTED_TEMPLATES = {
        TimeOfDay.EARLY_MORNING: [
            "Good morning, {speaker}. Your screen is locked. Let me verify your voice and unlock it so I can {action}.",
            "Early start today, {speaker}. I see your screen is locked. Verifying your voice now to {action}.",
            "Morning, {speaker}. Screen is locked - let me authenticate you quickly so we can {action}.",
        ],
        TimeOfDay.MORNING: [
            "Your screen is locked, {speaker}. Let me verify your voice and unlock it so I can {action}.",
            "I notice your screen is locked. Authenticating you now so I can {action}.",
            "{speaker}, your screen needs unlocking. Verifying your voice to {action}.",
        ],
        TimeOfDay.AFTERNOON: [
            "Screen is locked, {speaker}. Let me verify your voice and unlock it so I can {action}.",
            "Your screen is locked. I'll authenticate you quickly so we can {action}.",
            "{speaker}, I need to verify your voice to unlock the screen and {action}.",
        ],
        TimeOfDay.EVENING: [
            "Your screen is locked, {speaker}. Verifying your voice now so I can {action}.",
            "Evening, {speaker}. Screen is locked - authenticating you so I can {action}.",
            "Let me verify your voice and unlock the screen so I can {action}.",
        ],
        TimeOfDay.NIGHT: [
            "Working late, {speaker}? Your screen is locked. Let me verify and unlock it so I can {action}.",
            "Night mode active. Verifying your voice to unlock and {action}.",
            "{speaker}, screen is locked. Quick authentication, then I'll {action}.",
        ],
        TimeOfDay.LATE_NIGHT: [
            "Burning the midnight oil, {speaker}? Let me verify your voice and unlock so I can {action}.",
            "Late night session. Authenticating you quickly to {action}.",
            "{speaker}, verifying your voice for this late-night request to {action}.",
        ],
    }

    # ─────────────────────────────────────────────────────────────────────────
    # VBI STAGE MESSAGES
    # ─────────────────────────────────────────────────────────────────────────
    VBI_STAGE_TEMPLATES = {
        CAIStage.VBI_STARTING: [
            "Analyzing your voice...",
            "Processing voice biometrics...",
            "Verifying your voiceprint...",
        ],
        CAIStage.VBI_EXTRACTING: [
            "Extracting voice patterns...",
            "Capturing acoustic features...",
            "Processing voice signature...",
        ],
        CAIStage.VBI_COMPARING: [
            "Comparing with your profile...",
            "Matching voice patterns...",
            "Checking biometric signature...",
        ],
        CAIStage.VBI_VERIFYING: [
            "Finalizing verification...",
            "Confirming identity...",
            "Almost there...",
        ],
    }

    # ─────────────────────────────────────────────────────────────────────────
    # VBI SUCCESS MESSAGES (by confidence level)
    # ─────────────────────────────────────────────────────────────────────────
    VBI_SUCCESS_TEMPLATES = {
        ConfidenceLevel.INSTANT: [
            "Verified instantly, {speaker}. Unlocking now.",
            "Perfect match, {speaker}. Unlocking.",
            "Voice confirmed, {speaker}. Opening your screen.",
        ],
        ConfidenceLevel.HIGH: [
            "Voice verified, {speaker}. Unlocking now.",
            "Confirmed it's you, {speaker}. Unlocking.",
            "Authentication successful, {speaker}. Unlocking screen.",
        ],
        ConfidenceLevel.GOOD: [
            "Good match, {speaker}. Unlocking your screen.",
            "Voice confirmed, {speaker}. Unlocking now.",
            "Verified, {speaker}. Unlocking.",
        ],
        ConfidenceLevel.BORDERLINE: [
            "I'm fairly confident that's you, {speaker}. Unlocking now.",
            "Voice matches with {confidence}% certainty, {speaker}. Proceeding to unlock.",
            "Match confirmed, {speaker}. Unlocking your screen.",
        ],
    }

    # ─────────────────────────────────────────────────────────────────────────
    # VBI FAILURE MESSAGES (with intelligent retry guidance)
    # ─────────────────────────────────────────────────────────────────────────
    VBI_FAILURE_TEMPLATES = {
        "low_confidence": [
            "I'm having trouble verifying your voice. Could you try speaking a bit louder?",
            "The confidence is too low. Please try again, speaking clearly.",
            "I couldn't get a strong match. Try speaking closer to the microphone.",
        ],
        "noisy_environment": [
            "There's too much background noise. Could you move to a quieter spot?",
            "I'm picking up a lot of ambient sound. Try again in a quieter environment.",
            "The background noise is interfering. Let's try again when it's quieter.",
        ],
        "voice_sounds_different": [
            "Your voice sounds different today. Are you feeling okay? Try speaking naturally.",
            "I'm noticing some differences in your voice. Please try again.",
            "Your voice pattern seems off. Take a breath and try speaking normally.",
        ],
        "no_audio": [
            "I didn't receive any audio. Please make sure your microphone is working.",
            "No voice detected. Check that your microphone is enabled and try again.",
            "I couldn't hear you. Please check your microphone settings.",
        ],
        "unknown_speaker": [
            "I don't recognize this voice. If you're the owner, please re-enroll.",
            "This voice doesn't match any registered profiles.",
            "Voice not recognized. Authentication denied.",
        ],
        "replay_suspected": [
            "Security alert: This appears to be a recording, not a live voice.",
            "I detected characteristics of a playback. Please speak live.",
            "Authentication blocked: Possible replay attack detected.",
        ],
        "generic": [
            "Voice verification failed. Please try again.",
            "I couldn't verify your identity. Try speaking clearly.",
            "Authentication unsuccessful. Please try again.",
        ],
    }

    # ─────────────────────────────────────────────────────────────────────────
    # UNLOCK SUCCESS MESSAGES
    # ─────────────────────────────────────────────────────────────────────────
    UNLOCK_SUCCESS_TEMPLATES = [
        "Screen unlocked. Now {action}.",
        "Unlocked. {action_capitalized}.",
        "You're in, {speaker}. {action_capitalized}.",
        "Access granted. Now {action}.",
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # CONTINUATION MESSAGES
    # ─────────────────────────────────────────────────────────────────────────
    CONTINUATION_START_TEMPLATES = [
        "Now {action}...",
        "{action_capitalized}...",
        "Working on {action}...",
        "Processing your request to {action}...",
    ]

    CONTINUATION_COMPLETE_TEMPLATES = [
        "Done, {speaker}. {result}",
        "Completed. {result}",
        "There you go, {speaker}. {result}",
        "All set. {result}",
    ]

    # ─────────────────────────────────────────────────────────────────────────
    # MESSAGE GENERATION METHODS
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def generate_lock_detected(cls, ctx: CAIContext) -> str:
        """Generate context-aware lock detected message."""
        templates = cls.LOCK_DETECTED_TEMPLATES.get(ctx.time_of_day, cls.LOCK_DETECTED_TEMPLATES[TimeOfDay.MORNING])
        template = random.choice(templates)

        return template.format(
            speaker=ctx.speaker_name,
            action=ctx.continuation_action or "complete your request"
        )

    @classmethod
    def generate_vbi_stage(cls, stage: CAIStage) -> str:
        """Generate VBI stage message."""
        templates = cls.VBI_STAGE_TEMPLATES.get(stage, ["Processing..."])
        return random.choice(templates)

    @classmethod
    def generate_vbi_success(cls, ctx: CAIContext) -> str:
        """Generate confidence-appropriate success message."""
        level = ctx.confidence_level
        templates = cls.VBI_SUCCESS_TEMPLATES.get(level, cls.VBI_SUCCESS_TEMPLATES[ConfidenceLevel.HIGH])
        template = random.choice(templates)

        return template.format(
            speaker=ctx.speaker_name,
            confidence=int(ctx.vbi_confidence * 100)
        )

    @classmethod
    def generate_vbi_failure(cls, ctx: CAIContext, failure_reason: str = "generic") -> str:
        """Generate intelligent failure message with retry guidance."""
        # Determine failure category
        reason_key = "generic"
        failure_lower = failure_reason.lower()

        if "noise" in failure_lower or "snr" in failure_lower:
            reason_key = "noisy_environment"
        elif "different" in failure_lower or "sick" in failure_lower:
            reason_key = "voice_sounds_different"
        elif "no audio" in failure_lower or "silent" in failure_lower:
            reason_key = "no_audio"
        elif "unknown" in failure_lower or "not recognized" in failure_lower:
            reason_key = "unknown_speaker"
        elif "replay" in failure_lower or "recording" in failure_lower:
            reason_key = "replay_suspected"
        elif "confidence" in failure_lower or "low" in failure_lower:
            reason_key = "low_confidence"

        templates = cls.VBI_FAILURE_TEMPLATES.get(reason_key, cls.VBI_FAILURE_TEMPLATES["generic"])
        return random.choice(templates)

    @classmethod
    def generate_unlock_success(cls, ctx: CAIContext) -> str:
        """Generate unlock success message."""
        template = random.choice(cls.UNLOCK_SUCCESS_TEMPLATES)
        action = ctx.continuation_action or "completing your request"

        return template.format(
            speaker=ctx.speaker_name,
            action=action,
            action_capitalized=action.capitalize() if action else "Processing"
        )

    @classmethod
    def generate_continuation_start(cls, ctx: CAIContext) -> str:
        """Generate continuation start message."""
        template = random.choice(cls.CONTINUATION_START_TEMPLATES)
        action = ctx.continuation_action or "processing your request"

        return template.format(
            action=action,
            action_capitalized=action.capitalize() if action else "Processing"
        )

    @classmethod
    def generate_continuation_complete(cls, ctx: CAIContext, result: str) -> str:
        """Generate completion message."""
        template = random.choice(cls.CONTINUATION_COMPLETE_TEMPLATES)

        return template.format(
            speaker=ctx.speaker_name,
            result=result or "Your request has been completed."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CAI VOICE FEEDBACK MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class CAIVoiceFeedbackManager:
    """
    Intelligent voice feedback manager for CAI flow.

    Provides:
    - Async, non-blocking voice output
    - Parallel operations where possible
    - Dynamic message generation
    - Stage-by-stage narration
    - Error handling with fallbacks
    - Real-time progress updates
    """

    _instance: Optional["CAIVoiceFeedbackManager"] = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __init__(self):
        """Initialize the manager."""
        self._voice_lock = asyncio.Lock()
        self._is_speaking = False
        self._current_speech_process: Optional[asyncio.subprocess.Process] = None
        self._background_tasks: Set[asyncio.Task] = set()

        # Configuration
        self._voice_name = "Daniel"
        self._default_rate = 175
        self._enabled = True
        self._muted = False

        # Voice communicator integration (optional)
        self._voice_communicator = None

        # Metrics
        self._messages_spoken = 0
        self._total_speech_time_ms = 0.0

        logger.info("[CAI-VOICE] CAIVoiceFeedbackManager initialized")

    @classmethod
    async def get_instance(cls) -> "CAIVoiceFeedbackManager":
        """Get or create singleton instance."""
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
                    await cls._instance._initialize()
        return cls._instance

    async def _initialize(self) -> None:
        """Async initialization."""
        # Try to get voice communicator for advanced features
        try:
            from agi_os.realtime_voice_communicator import get_voice_communicator
            self._voice_communicator = await asyncio.wait_for(
                get_voice_communicator(),
                timeout=2.0
            )
            logger.info("[CAI-VOICE] Voice communicator integration active")
        except Exception as e:
            logger.debug(f"[CAI-VOICE] Voice communicator not available: {e}")
            self._voice_communicator = None

    @property
    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        return self._is_speaking

    # ─────────────────────────────────────────────────────────────────────────
    # CORE SPEECH METHODS
    # ─────────────────────────────────────────────────────────────────────────

    async def _speak_async(
        self,
        text: str,
        rate: int = 175,
        wait: bool = True,
        interrupt: bool = False
    ) -> bool:
        """
        Speak text asynchronously using macOS say command.

        Args:
            text: Text to speak
            rate: Words per minute
            wait: Wait for speech to complete
            interrupt: Interrupt current speech

        Returns:
            True if speech started/completed successfully
        """
        if not self._enabled or self._muted or not text:
            return False

        # Interrupt current speech if requested
        if interrupt and self._current_speech_process:
            try:
                self._current_speech_process.terminate()
                await asyncio.wait_for(
                    self._current_speech_process.wait(),
                    timeout=1.0
                )
            except Exception:
                pass

        async with self._voice_lock:
            try:
                self._is_speaking = True
                start_time = time.time()

                logger.info(f"[CAI-VOICE] Speaking: {text[:80]}...")

                # Route through safe_say for AudioBus-safe speech
                if _safe_say is not None:
                    result = await _safe_say(
                        text,
                        voice=self._voice_name,
                        rate=rate,
                        wait=wait,
                        timeout=30.0,
                        source="cai_voice_feedback",
                    )
                else:
                    # Fallback: say -o tempfile + afplay (NEVER raw say)
                    import tempfile as _tmpmod
                    _fd, _tmp = _tmpmod.mkstemp(suffix=".aiff", prefix="jarvis_cai_")
                    os.close(_fd)
                    try:
                        self._current_speech_process = await asyncio.create_subprocess_exec(
                            "say", "-v", self._voice_name, "-r", str(rate),
                            "-o", _tmp, text,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(self._current_speech_process.wait(), timeout=30.0)
                        if wait:
                            _afp = await asyncio.create_subprocess_exec(
                                "afplay", _tmp,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                            )
                            await asyncio.wait_for(_afp.wait(), timeout=30.0)
                        result = True
                    finally:
                        try:
                            os.unlink(_tmp)
                        except OSError:
                            pass

                elapsed = (time.time() - start_time) * 1000
                self._total_speech_time_ms += elapsed
                self._messages_spoken += 1

                logger.debug(f"[CAI-VOICE] Speech completed in {elapsed:.0f}ms")
                return result

            except asyncio.TimeoutError:
                logger.warning("[CAI-VOICE] Speech timeout")
                if self._current_speech_process:
                    self._current_speech_process.terminate()
                return False
            except Exception as e:
                logger.error(f"[CAI-VOICE] Speech error: {e}")
                return False
            finally:
                self._is_speaking = False
                self._current_speech_process = None

    async def _speak_fire_and_forget(self, text: str, rate: int = 175) -> None:
        """
        Speak text without waiting (fire-and-forget).
        Uses bulletproof task scheduling to prevent GC.
        """
        async def _speak():
            await self._speak_async(text, rate, wait=True, interrupt=False)

        task = asyncio.create_task(_speak(), name=f"cai_voice_{time.time()}")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ─────────────────────────────────────────────────────────────────────────
    # CAI FLOW ANNOUNCEMENTS
    # ─────────────────────────────────────────────────────────────────────────

    async def announce_lock_detected(self, ctx: CAIContext) -> None:
        """
        Announce that screen lock was detected and VBI will verify.
        This is the first verbal feedback in the CAI flow.
        """
        message = DynamicMessageGenerator.generate_lock_detected(ctx)
        await self._speak_async(message, rate=170, wait=True)

    async def announce_vbi_start(self, ctx: CAIContext) -> None:
        """
        Announce VBI verification is starting.
        Brief message - don't want to talk over the verification.
        """
        message = DynamicMessageGenerator.generate_vbi_stage(CAIStage.VBI_STARTING)
        # Fire and forget - VBI can run in parallel
        await self._speak_fire_and_forget(message)

    async def announce_vbi_progress(
        self,
        ctx: CAIContext,
        stage: CAIStage,
        progress: float = 0.0
    ) -> None:
        """
        Announce VBI progress (fire-and-forget for non-blocking).
        Only announce key stages to avoid talking too much.
        """
        # Only narrate key stages
        if stage in (CAIStage.VBI_COMPARING, CAIStage.VBI_VERIFYING):
            message = DynamicMessageGenerator.generate_vbi_stage(stage)
            await self._speak_fire_and_forget(message)

    async def announce_verification_result(
        self,
        ctx: CAIContext,
        success: bool,
        confidence: float = 0.0,
        failure_reason: str = ""
    ) -> None:
        """
        Announce VBI verification result.
        Critical message - wait for completion.
        """
        ctx.vbi_confidence = confidence

        if success:
            message = DynamicMessageGenerator.generate_vbi_success(ctx)
        else:
            message = DynamicMessageGenerator.generate_vbi_failure(ctx, failure_reason)

        await self._speak_async(message, rate=175, wait=True)

    async def announce_unlock_success(self, ctx: CAIContext) -> None:
        """
        Announce screen unlock success and upcoming continuation.
        Critical message - user needs to know unlock worked.
        """
        ctx.was_unlocked = True
        message = DynamicMessageGenerator.generate_unlock_success(ctx)
        await self._speak_async(message, rate=180, wait=True)

    async def announce_unlock_failed(
        self,
        ctx: CAIContext,
        reason: str = ""
    ) -> None:
        """
        Announce unlock failure with reason.
        """
        message = f"I couldn't unlock your screen. {reason}" if reason else "I couldn't unlock your screen."
        await self._speak_async(message, rate=170, wait=True)

    async def announce_continuation_start(self, ctx: CAIContext) -> None:
        """
        Announce continuation task is starting.
        Brief message - the action speaks for itself.
        """
        message = DynamicMessageGenerator.generate_continuation_start(ctx)
        # Fire and forget - let the continuation run
        await self._speak_fire_and_forget(message)

    async def announce_task_completion(
        self,
        ctx: CAIContext,
        result: str = ""
    ) -> None:
        """
        Announce task completion with results.
        """
        message = DynamicMessageGenerator.generate_continuation_complete(ctx, result)
        await self._speak_async(message, rate=175, wait=True)

    async def announce_error(
        self,
        ctx: CAIContext,
        error_message: str = ""
    ) -> None:
        """
        Announce an error with helpful guidance.
        """
        ctx.error_message = error_message

        # Generate helpful error message
        if "timeout" in error_message.lower():
            message = "The operation is taking too long. Please try again."
        elif "connection" in error_message.lower():
            message = "I'm having trouble connecting. Please check your network."
        else:
            message = f"Something went wrong. {error_message}" if error_message else "Something went wrong. Please try again."

        await self._speak_async(message, rate=165, wait=True)

    # ─────────────────────────────────────────────────────────────────────────
    # CONVENIENCE METHODS FOR FULL FLOW
    # ─────────────────────────────────────────────────────────────────────────

    async def run_full_cai_flow(
        self,
        ctx: CAIContext,
        vbi_callback: Callable[[], Any],
        unlock_callback: Callable[[], Any],
        continuation_callback: Callable[[], Any]
    ) -> Dict[str, Any]:
        """
        Run the full CAI flow with verbal transparency at each step.

        This is a convenience method that orchestrates:
        1. Lock detection announcement
        2. VBI verification with progress
        3. Unlock with confirmation
        4. Continuation with completion

        Args:
            ctx: CAI context
            vbi_callback: Async function to perform VBI verification
            unlock_callback: Async function to unlock screen
            continuation_callback: Async function to execute continuation

        Returns:
            Dict with flow results
        """
        result = {
            "success": False,
            "vbi_success": False,
            "unlock_success": False,
            "continuation_success": False,
            "error": None,
            "total_time_ms": 0.0
        }

        start_time = time.time()

        try:
            # Step 1: Announce lock detected
            await self.announce_lock_detected(ctx)

            # Step 2: VBI verification (with parallel announcement)
            await self.announce_vbi_start(ctx)

            vbi_result = await vbi_callback()

            if not vbi_result.get("success", False):
                await self.announce_verification_result(
                    ctx,
                    success=False,
                    confidence=vbi_result.get("confidence", 0.0),
                    failure_reason=vbi_result.get("error", "")
                )
                result["error"] = "VBI verification failed"
                return result

            # VBI success
            result["vbi_success"] = True
            ctx.vbi_confidence = vbi_result.get("confidence", 0.85)
            await self.announce_verification_result(ctx, success=True, confidence=ctx.vbi_confidence)

            # Step 3: Unlock screen
            unlock_result = await unlock_callback()

            if not unlock_result.get("success", False):
                await self.announce_unlock_failed(ctx, unlock_result.get("error", ""))
                result["error"] = "Screen unlock failed"
                return result

            # Unlock success
            result["unlock_success"] = True
            await self.announce_unlock_success(ctx)

            # Step 4: Continuation
            await self.announce_continuation_start(ctx)

            continuation_result = await continuation_callback()

            if continuation_result.get("success", False):
                result["continuation_success"] = True
                await self.announce_task_completion(
                    ctx,
                    continuation_result.get("response", "")
                )
            else:
                await self.announce_error(ctx, continuation_result.get("error", ""))

            result["success"] = result["continuation_success"]

        except Exception as e:
            logger.error(f"[CAI-VOICE] Flow error: {e}")
            result["error"] = str(e)
            await self.announce_error(ctx, str(e))

        finally:
            result["total_time_ms"] = (time.time() - start_time) * 1000

        return result

    # ─────────────────────────────────────────────────────────────────────────
    # CONTROL METHODS
    # ─────────────────────────────────────────────────────────────────────────

    def mute(self) -> None:
        """Mute all voice output."""
        self._muted = True
        logger.info("[CAI-VOICE] Voice muted")

    def unmute(self) -> None:
        """Unmute voice output."""
        self._muted = False
        logger.info("[CAI-VOICE] Voice unmuted")

    async def stop_speaking(self) -> None:
        """Stop current speech immediately."""
        if self._current_speech_process:
            try:
                self._current_speech_process.terminate()
                await self._current_speech_process.wait()
            except Exception:
                pass
        self._is_speaking = False

    def get_metrics(self) -> Dict[str, Any]:
        """Get voice metrics."""
        return {
            "messages_spoken": self._messages_spoken,
            "total_speech_time_ms": self._total_speech_time_ms,
            "average_speech_time_ms": (
                self._total_speech_time_ms / self._messages_spoken
                if self._messages_spoken > 0 else 0
            ),
            "is_speaking": self._is_speaking,
            "is_muted": self._muted,
            "background_tasks": len(self._background_tasks),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SINGLETON ACCESSOR
# ═══════════════════════════════════════════════════════════════════════════════

async def get_cai_voice_manager() -> CAIVoiceFeedbackManager:
    """Get the CAI Voice Feedback Manager singleton."""
    return await CAIVoiceFeedbackManager.get_instance()


# ═══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTION FOR JARVIS_VOICE_API INTEGRATION
# ═══════════════════════════════════════════════════════════════════════════════

async def speak_cai_message(message: str, wait: bool = True) -> bool:
    """
    Convenience function to speak a CAI message.
    Can be used directly from jarvis_voice_api.py.

    Args:
        message: Text to speak
        wait: Wait for speech to complete

    Returns:
        True if speech was successful
    """
    try:
        manager = await get_cai_voice_manager()
        if wait:
            return await manager._speak_async(message, rate=175, wait=True)
        else:
            await manager._speak_fire_and_forget(message)
            return True
    except Exception as e:
        logger.error(f"[CAI-VOICE] speak_cai_message error: {e}")
        # Fallback: route through safe_say for AudioBus safety
        try:
            if _safe_say is not None:
                return await _safe_say(
                    message,
                    voice="Daniel",
                    wait=wait,
                    timeout=30.0,
                    source="cai_voice_fallback",
                )
            else:
                # Last resort: say -o tempfile + afplay (NEVER raw say)
                import tempfile as _tmpmod
                _fd, _tmp = _tmpmod.mkstemp(suffix=".aiff", prefix="jarvis_cai_fb_")
                os.close(_fd)
                try:
                    _proc = await asyncio.create_subprocess_exec(
                        "say", "-v", "Daniel", "-o", _tmp, message,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(_proc.wait(), timeout=30.0)
                    if wait:
                        _afp = await asyncio.create_subprocess_exec(
                            "afplay", _tmp,
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await asyncio.wait_for(_afp.wait(), timeout=30.0)
                    return True
                finally:
                    try:
                        os.unlink(_tmp)
                    except OSError:
                        pass
        except Exception:
            return False


def extract_continuation_action(text: str) -> str:
    """
    Extract the continuation action from command text.
    Used to generate contextual messages like "search for dogs".

    Args:
        text: Command text

    Returns:
        Extracted action string
    """
    import re

    text_lower = text.lower().strip()

    patterns = [
        (r"search\s+(?:for\s+)?(.+)", lambda m: f"search for {m.group(1)}"),
        (r"google\s+(.+)", lambda m: f"search for {m.group(1)}"),
        (r"find\s+(.+)", lambda m: f"find {m.group(1)}"),
        (r"open\s+(.+)", lambda m: f"open {m.group(1)}"),
        (r"launch\s+(.+)", lambda m: f"launch {m.group(1)}"),
        (r"start\s+(.+)", lambda m: f"start {m.group(1)}"),
        (r"play\s+(.+)", lambda m: f"play {m.group(1)}"),
        (r"show\s+(?:me\s+)?(.+)", lambda m: f"show you {m.group(1)}"),
        (r"tell\s+(?:me\s+)?(.+)", lambda m: f"tell you {m.group(1)}"),
        (r"what\s+(?:is|are)\s+(.+)", lambda m: f"look up {m.group(1)}"),
        (r"how\s+(?:do|to)\s+(.+)", lambda m: f"explain {m.group(1)}"),
    ]

    for pattern, extractor in patterns:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            return extractor(match)

    return "complete your request"

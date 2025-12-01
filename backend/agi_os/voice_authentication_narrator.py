"""
JARVIS AGI OS - Voice Authentication Narrator

Advanced voice feedback system for authentication that implements:
- Progressive confidence communication
- Environmental awareness narration
- Security storytelling
- Learning acknowledgment
- Multi-attempt handling
- Time-aware contextual greetings

This module transforms voice authentication from a simple biometric check into
an intelligent, adaptive, conversational security system.

Usage:
    from agi_os.voice_authentication_narrator import (
        VoiceAuthNarrator,
        get_auth_narrator,
    )

    narrator = await get_auth_narrator()

    # After authentication attempt
    await narrator.narrate_authentication_result(auth_result)

    # After failed attempt
    await narrator.narrate_retry_guidance(auth_context)
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ConfidenceLevel(Enum):
    """Authentication confidence levels."""
    VERY_HIGH = "very_high"      # >95% - instant recognition
    HIGH = "high"                # 90-95% - confident match
    GOOD = "good"                # 85-90% - clear match
    BORDERLINE = "borderline"    # 80-85% - slight doubt
    LOW = "low"                  # 75-80% - uncertain
    FAILED = "failed"            # <75% - not matched


class EnvironmentType(Enum):
    """Detected environment types."""
    QUIET_HOME = "quiet_home"
    NOISY_CAFE = "noisy_cafe"
    OFFICE = "office"
    OUTDOOR = "outdoor"
    UNKNOWN = "unknown"


class TimeOfDay(Enum):
    """Time of day categories."""
    EARLY_MORNING = "early_morning"    # 5-7 AM
    MORNING = "morning"                # 7-12 PM
    AFTERNOON = "afternoon"            # 12-5 PM
    EVENING = "evening"                # 5-9 PM
    NIGHT = "night"                    # 9 PM - 12 AM
    LATE_NIGHT = "late_night"          # 12-5 AM


@dataclass
class AuthenticationContext:
    """Context for an authentication attempt."""
    voice_confidence: float
    behavioral_confidence: float = 0.0
    context_confidence: float = 0.0
    fused_confidence: float = 0.0

    # Environment
    environment_type: EnvironmentType = EnvironmentType.UNKNOWN
    snr_db: float = 0.0  # Signal-to-noise ratio

    # Behavioral factors
    is_typical_time: bool = True
    last_unlock_hours_ago: float = 0.0
    consecutive_failures: int = 0

    # Voice analysis
    voice_sounds_different: bool = False
    suspected_reason: Optional[str] = None  # tired, sick, microphone, etc.

    # Security
    is_replay_suspected: bool = False
    is_unknown_speaker: bool = False

    # Metadata
    attempt_number: int = 1
    timestamp: datetime = field(default_factory=datetime.now)
    location: Optional[str] = None
    device: Optional[str] = None


@dataclass
class AuthenticationResult:
    """Result of an authentication attempt."""
    success: bool
    context: AuthenticationContext
    unlock_performed: bool = False
    verification_method: str = "voice"
    reasoning: str = ""

    # Decision details
    decision_factors: Dict[str, float] = field(default_factory=dict)

    # For learning
    learned_something: bool = False
    learning_note: Optional[str] = None


class VoiceAuthNarrator:
    """
    Advanced voice narrator for authentication interactions.

    Provides human-like, contextual voice feedback that makes authentication
    feel like interacting with a trusted security guard who knows you.

    The narrator dynamically identifies the owner via voice biometrics
    instead of using hardcoded names, integrating with OwnerIdentityService.
    """

    def __init__(self, voice_communicator=None, owner_identity_service=None):
        """Initialize the narrator.

        Args:
            voice_communicator: RealTimeVoiceCommunicator instance (optional)
            owner_identity_service: OwnerIdentityService instance (optional)
        """
        self._voice = voice_communicator
        self._identity_service = owner_identity_service

        # Dynamic user name - fetched from identity service
        # Cached for performance, refreshed on voice verification
        self._cached_user_name: Optional[str] = None
        self._name_cache_time: Optional[datetime] = None
        self._name_cache_ttl = timedelta(minutes=30)

        # Statistics tracking
        self._stats = {
            'total_unlocks': 0,
            'instant_recognitions': 0,
            'needed_clarification': 0,
            'false_positives': 0,
            'replay_attacks_blocked': 0,
        }

        # History for pattern recognition
        self._recent_authentications: List[AuthenticationResult] = []
        self._max_history = 100

        # Learned environment profiles
        self._environment_profiles: Dict[str, Dict[str, Any]] = {}

        # Configuration
        self._config = {
            'use_name': True,
            'mention_confidence': False,  # Don't mention numbers unless borderline
            'mention_learning': True,
            'celebration_milestones': [100, 500, 1000],
        }

        logger.info("VoiceAuthNarrator initialized (dynamic identity mode)")

    async def set_voice_communicator(self, voice) -> None:
        """Set the voice communicator."""
        self._voice = voice

    async def set_identity_service(self, identity_service) -> None:
        """Set the owner identity service for dynamic user identification."""
        self._identity_service = identity_service
        # Clear cached name to force refresh
        self._cached_user_name = None
        logger.info("Identity service connected to VoiceAuthNarrator")

    async def _get_user_name(self, audio_data: Optional[bytes] = None) -> str:
        """
        Get the current user's name dynamically via voice biometrics.

        This method integrates with OwnerIdentityService to provide
        dynamic, voice-verified user identification.

        Args:
            audio_data: Optional audio for real-time verification

        Returns:
            User's name for greeting
        """
        # If we have audio, always do fresh verification
        if audio_data and self._identity_service:
            try:
                name = await self._identity_service.get_owner_name(
                    use_first_name=True,
                    audio_data=audio_data
                )
                self._cached_user_name = name
                self._name_cache_time = datetime.now()
                return name
            except Exception as e:
                logger.warning(f"Voice identity lookup failed: {e}")

        # Check if cached name is still valid
        if self._cached_user_name and self._name_cache_time:
            age = datetime.now() - self._name_cache_time
            if age < self._name_cache_ttl:
                return self._cached_user_name

        # Fetch from identity service (no audio verification)
        if self._identity_service:
            try:
                name = await self._identity_service.get_greeting_name()
                self._cached_user_name = name
                self._name_cache_time = datetime.now()
                return name
            except Exception as e:
                logger.warning(f"Identity service lookup failed: {e}")

        # Fallback - try to get macOS username
        return await self._get_fallback_name()

    async def _get_fallback_name(self) -> str:
        """Get fallback name from macOS or generic greeting."""
        import os
        import subprocess

        try:
            # Try macOS dscl to get real name
            username = os.environ.get('USER') or os.getlogin()
            result = subprocess.run(
                ['dscl', '.', '-read', f'/Users/{username}', 'RealName'],
                capture_output=True,
                text=True,
                timeout=2
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    full_name = lines[1].strip()
                    return full_name.split()[0]  # First name
        except Exception:
            pass

        # Ultimate fallback
        return "there"

    def set_user_name(self, name: str) -> None:
        """
        Manually set the user's name (legacy support).

        Note: This is deprecated. Use set_identity_service() instead
        for dynamic voice-based identification.
        """
        self._cached_user_name = name
        self._name_cache_time = datetime.now()
        logger.info(f"User name manually set to: {name} (consider using identity service)")

    @property
    def _user_name(self) -> str:
        """
        Legacy property accessor for backward compatibility.

        Returns cached name synchronously. For async dynamic lookup,
        use _get_user_name() method instead.
        """
        if self._cached_user_name:
            return self._cached_user_name
        return "there"  # Safe fallback for sync access

    @_user_name.setter
    def _user_name(self, value: str) -> None:
        """Legacy setter for backward compatibility."""
        self._cached_user_name = value
        self._name_cache_time = datetime.now()

    def _get_time_of_day(self) -> TimeOfDay:
        """Get current time of day category."""
        hour = datetime.now().hour

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

    def _get_confidence_level(self, confidence: float) -> ConfidenceLevel:
        """Convert numeric confidence to level."""
        if confidence >= 0.95:
            return ConfidenceLevel.VERY_HIGH
        elif confidence >= 0.90:
            return ConfidenceLevel.HIGH
        elif confidence >= 0.85:
            return ConfidenceLevel.GOOD
        elif confidence >= 0.80:
            return ConfidenceLevel.BORDERLINE
        elif confidence >= 0.75:
            return ConfidenceLevel.LOW
        else:
            return ConfidenceLevel.FAILED

    async def narrate_authentication_result(
        self,
        result: AuthenticationResult
    ) -> None:
        """
        Narrate the authentication result with appropriate context.

        This is the main entry point for authentication feedback.
        """
        if not self._voice:
            logger.warning("No voice communicator set, skipping narration")
            return

        # Update statistics
        self._update_stats(result)

        # Store in history
        self._recent_authentications.append(result)
        if len(self._recent_authentications) > self._max_history:
            self._recent_authentications.pop(0)

        # Generate appropriate response
        if result.success:
            await self._narrate_success(result)
        else:
            await self._narrate_failure(result)

        # Check for milestones
        await self._check_milestones()

    async def _narrate_success(self, result: AuthenticationResult) -> None:
        """Narrate a successful authentication."""
        ctx = result.context
        confidence_level = self._get_confidence_level(ctx.fused_confidence or ctx.voice_confidence)
        time_of_day = self._get_time_of_day()

        # Build the response based on confidence level
        if confidence_level == ConfidenceLevel.VERY_HIGH:
            response = await self._build_instant_recognition_response(ctx, time_of_day)
        elif confidence_level == ConfidenceLevel.HIGH:
            response = await self._build_confident_response(ctx, time_of_day)
        elif confidence_level == ConfidenceLevel.GOOD:
            response = await self._build_good_response(ctx, time_of_day)
        elif confidence_level == ConfidenceLevel.BORDERLINE:
            response = await self._build_borderline_success_response(ctx, time_of_day, result)
        else:
            # Multi-factor saved it
            response = await self._build_multifactor_success_response(ctx, time_of_day, result)

        # Add learning acknowledgment if applicable
        if result.learned_something and self._config['mention_learning']:
            response += f" {result.learning_note}"

        # Speak it
        await self._speak(response, self._get_voice_mode_for_confidence(confidence_level))

    async def _narrate_failure(self, result: AuthenticationResult) -> None:
        """Narrate a failed authentication."""
        ctx = result.context

        # Determine the type of failure
        if ctx.is_replay_suspected:
            await self._narrate_replay_attack(ctx)
        elif ctx.is_unknown_speaker:
            await self._narrate_unknown_speaker(ctx)
        elif ctx.voice_sounds_different:
            await self._narrate_voice_difference(ctx)
        elif ctx.snr_db < 12:
            await self._narrate_noise_issue(ctx)
        else:
            await self._narrate_general_failure(ctx)

    async def _build_instant_recognition_response(
        self,
        ctx: AuthenticationContext,
        time_of_day: TimeOfDay
    ) -> str:
        """Build response for instant recognition (>95% confidence)."""
        name = await self._get_user_name() if self._config['use_name'] else ""

        greetings = {
            TimeOfDay.EARLY_MORNING: [
                f"Good morning, {name}. You're up early.",
                f"Morning, {name}.",
            ],
            TimeOfDay.MORNING: [
                f"Good morning, {name}.",
                f"Morning, {name}. Ready to start the day?",
            ],
            TimeOfDay.AFTERNOON: [
                f"Good afternoon, {name}.",
                f"Afternoon, {name}. Unlocking for you.",
            ],
            TimeOfDay.EVENING: [
                f"Good evening, {name}.",
                f"Evening, {name}.",
            ],
            TimeOfDay.NIGHT: [
                f"Still at it, {name}? Unlocking now.",
                f"Evening, {name}. Unlocking for you.",
            ],
            TimeOfDay.LATE_NIGHT: [
                f"Burning the midnight oil, {name}? Unlocking for you.",
                f"Late night session, {name}? Hope you're getting some rest soon.",
            ],
        }

        options = greetings.get(time_of_day, [f"Unlocking for you, {name}."])
        return random.choice(options).strip()

    async def _build_confident_response(
        self,
        ctx: AuthenticationContext,
        time_of_day: TimeOfDay
    ) -> str:
        """Build response for confident match (90-95%)."""
        name = await self._get_user_name() if self._config['use_name'] else ""

        responses = [
            f"Of course, {name}. Unlocking now.",
            f"Verified. Unlocking for you, {name}.",
            f"Welcome back, {name}.",
        ]

        return random.choice(responses).strip()

    async def _build_good_response(
        self,
        ctx: AuthenticationContext,
        time_of_day: TimeOfDay
    ) -> str:
        """Build response for good match (85-90%)."""
        name = await self._get_user_name() if self._config['use_name'] else ""

        # Slight pause acknowledgment
        responses = [
            f"One moment... yes, verified. Unlocking for you, {name}.",
            f"Good. Unlocking now, {name}.",
        ]

        return random.choice(responses).strip()

    async def _build_borderline_success_response(
        self,
        ctx: AuthenticationContext,
        time_of_day: TimeOfDay,
        result: AuthenticationResult
    ) -> str:
        """Build response for borderline success (80-85%)."""
        name = await self._get_user_name() if self._config['use_name'] else ""

        # Acknowledge the difficulty but confirm success
        if ctx.voice_sounds_different:
            reason = ctx.suspected_reason or "different"
            return f"Your voice sounds {reason} today, {name}, but your patterns match. Unlocking now."
        elif ctx.snr_db < 15:
            return f"Voice confirmation was a bit unclear due to background noise, but I'm confident it's you, {name}. Unlocking."
        else:
            # Multi-factor saved it
            factors = []
            if ctx.behavioral_confidence > 0.9:
                factors.append("your behavioral patterns match perfectly")
            if ctx.is_typical_time:
                factors.append("this is your typical unlock time")

            factor_text = " and ".join(factors) if factors else "other factors check out"
            return f"Voice confidence was a bit lower than usual, but {factor_text}. Unlocking for you, {name}."

    async def _build_multifactor_success_response(
        self,
        ctx: AuthenticationContext,
        time_of_day: TimeOfDay,
        result: AuthenticationResult
    ) -> str:
        """Build response when multi-factor saved a low voice confidence."""
        name = await self._get_user_name() if self._config['use_name'] else ""

        return (
            f"I couldn't verify your voice clearly, {name}, "
            f"but your behavioral patterns and context are perfect. "
            f"Unlocking based on multi-factor verification."
        )

    async def _narrate_replay_attack(self, ctx: AuthenticationContext) -> None:
        """Narrate detection of a replay attack."""
        response = (
            "Security alert: I detected characteristics consistent with a voice recording "
            "rather than a live person. Access denied. "
            "This attempt has been logged."
        )

        self._stats['replay_attacks_blocked'] += 1
        await self._speak(response, "urgent")

    async def _narrate_unknown_speaker(self, ctx: AuthenticationContext) -> None:
        """Narrate when an unknown speaker attempts unlock."""
        # Get the owner's name for the security message
        owner_name = await self._get_user_name()

        if ctx.consecutive_failures > 1:
            response = (
                "I don't recognize this voice. This device is voice-locked to "
                f"{owner_name}. Please stop trying, or I'll need to alert the owner."
            )
        else:
            response = (
                f"I don't recognize this voice. This device is voice-locked to {owner_name} only. "
                "If you need access, please ask them directly or use the password."
            )

        await self._speak(response, "notification")

    async def _narrate_voice_difference(self, ctx: AuthenticationContext) -> None:
        """Narrate when voice sounds different."""
        reason = ctx.suspected_reason

        if reason == "tired":
            response = "You sound tired. Could you try speaking a bit more clearly?"
        elif reason == "sick":
            response = (
                "Your voice sounds different today, hope you're feeling alright. "
                "Could you try once more, or would you prefer to use the password?"
            )
        elif reason == "microphone":
            response = (
                "I'm having trouble with the audio quality. "
                "Are you using a different microphone than usual? "
                "Try speaking directly into the main microphone."
            )
        else:
            response = (
                "I'm having trouble verifying your voice. "
                "Could you try again, speaking a bit louder and closer to the microphone?"
            )

        await self._speak(response, "conversational")

    async def _narrate_noise_issue(self, ctx: AuthenticationContext) -> None:
        """Narrate when background noise is the issue."""
        if ctx.consecutive_failures == 0:
            response = (
                "I'm having trouble hearing you clearly through the background noise. "
                "Could you try again, maybe speak a bit louder?"
            )
        elif ctx.consecutive_failures == 1:
            response = (
                "Still having difficulty with the noise. "
                "Can you move to a quieter spot, or speak right into the microphone?"
            )
        else:
            response = (
                "The background noise is making voice verification difficult. "
                "Would you like to use password unlock instead?"
            )

        await self._speak(response, "conversational")

    async def _narrate_general_failure(self, ctx: AuthenticationContext) -> None:
        """Narrate a general authentication failure."""
        attempt = ctx.attempt_number

        if attempt == 1:
            response = "Voice verification didn't succeed. Could you try once more?"
        elif attempt == 2:
            response = (
                "Still having trouble verifying. "
                "Try speaking your unlock phrase clearly and steadily."
            )
        elif attempt == 3:
            response = (
                "I'm not able to verify your voice after three attempts. "
                "Would you like to use password unlock instead, "
                "or shall I run a quick voice recalibration?"
            )
        else:
            response = (
                "Voice verification isn't working right now. "
                "Please use your password to unlock."
            )

        await self._speak(response, "conversational")

    async def narrate_retry_guidance(
        self,
        ctx: AuthenticationContext,
        specific_guidance: Optional[str] = None
    ) -> None:
        """Provide specific guidance for retry attempts."""
        if specific_guidance:
            await self._speak(specific_guidance, "conversational")
            return

        # Dynamic guidance based on context
        if ctx.snr_db < 12:
            guidance = "Speak closer to the microphone to help me hear you clearly."
        elif ctx.voice_sounds_different:
            guidance = "Take a breath and speak naturally, like you normally would."
        else:
            guidance = "Try saying your unlock phrase one more time."

        await self._speak(guidance, "conversational")

    async def narrate_learning_event(
        self,
        event_type: str,
        details: Dict[str, Any]
    ) -> None:
        """Narrate when the system learns something new."""
        if event_type == "new_environment":
            location = details.get('location', 'this location')
            response = (
                f"First time unlocking from {location}. "
                "I've learned your voice profile for this environment. "
                "Next time should be instant."
            )
        elif event_type == "voice_evolution":
            response = (
                "I've noticed your voice has evolved slightly over time. "
                "This is normal. I've updated my baseline to match."
            )
        elif event_type == "new_microphone":
            mic = details.get('microphone', 'this microphone')
            response = (
                f"I've learned your voice profile for {mic}. "
                "Recognition will be better next time."
            )
        else:
            response = "I've updated my understanding based on this interaction."

        await self._speak(response, "notification")

    async def narrate_security_incident(
        self,
        incident_type: str,
        details: Dict[str, Any]
    ) -> None:
        """Narrate security-related events."""
        if incident_type == "failed_attempts_while_away":
            count = details.get('count', 0)
            time_ago = details.get('time_ago', 'earlier')

            response = (
                f"Quick heads up: there were {count} failed unlock attempts while you were away, "
                f"{time_ago}. Different voice, not in my database. All attempts were denied. "
                "Would you like to review the details?"
            )
        elif incident_type == "suspicious_pattern":
            response = (
                "I've noticed an unusual pattern of access attempts. "
                "Everything is secure, but I wanted you to be aware."
            )
        else:
            response = "Security event logged. Everything is secure."

        await self._speak(response, "notification")

    async def _check_milestones(self) -> None:
        """Check and celebrate authentication milestones."""
        total = self._stats['total_unlocks']

        if total in self._config['celebration_milestones']:
            instant = self._stats['instant_recognitions']
            needed_help = self._stats['needed_clarification']
            blocked = self._stats['replay_attacks_blocked']

            response = (
                f"Fun fact: That was your {total}th successful voice unlock! "
                f"I've had {instant} instant recognitions, "
                f"{needed_help} needed clarification, "
                f"and I've blocked {blocked} suspicious attempts. "
                "Your voice authentication is rock solid."
            )

            await self._speak(response, "notification")

    # ============== Wake/Sleep Response Generation ==============

    async def narrate_laptop_wake(
        self,
        sleep_duration_seconds: float,
        upcoming_calendar_event: Optional[str] = None,
        is_first_wake_of_day: bool = False,
    ) -> None:
        """
        Generate and speak a dynamic, intelligent wake-from-sleep greeting.

        This method creates contextual greetings based on:
        - Time of day (morning, afternoon, evening, late night)
        - Sleep duration (quick break vs long sleep vs overnight)
        - Calendar context (upcoming meetings)
        - First wake of day vs returning from break
        - Day type (weekday vs weekend)

        Args:
            sleep_duration_seconds: How long the laptop was asleep
            upcoming_calendar_event: Optional next calendar event name
            is_first_wake_of_day: Whether this is the first wake today
        """
        name = await self._get_user_name()
        time_of_day = self._get_time_of_day()
        sleep_minutes = sleep_duration_seconds / 60
        sleep_hours = sleep_duration_seconds / 3600

        # Determine sleep context
        sleep_context = self._categorize_sleep_duration(sleep_duration_seconds)

        # Build contextual greeting
        greeting = await self._build_wake_greeting(
            name=name,
            time_of_day=time_of_day,
            sleep_context=sleep_context,
            sleep_minutes=sleep_minutes,
            sleep_hours=sleep_hours,
            upcoming_event=upcoming_calendar_event,
            is_first_wake=is_first_wake_of_day,
        )

        # Determine voice mode based on time
        voice_mode = self._get_wake_voice_mode(time_of_day, sleep_context)

        await self._speak(greeting, voice_mode)

    def _categorize_sleep_duration(self, seconds: float) -> str:
        """Categorize sleep duration for context-aware responses."""
        minutes = seconds / 60
        hours = seconds / 3600

        if minutes < 5:
            return "quick_lock"      # Just locked briefly
        elif minutes < 30:
            return "short_break"     # Coffee/bathroom break
        elif minutes < 120:
            return "extended_break"  # Lunch, meeting, etc.
        elif hours < 8:
            return "long_absence"    # Away for most of the day
        else:
            return "overnight"       # Sleep or next day

    async def _build_wake_greeting(
        self,
        name: str,
        time_of_day: TimeOfDay,
        sleep_context: str,
        sleep_minutes: float,
        sleep_hours: float,
        upcoming_event: Optional[str],
        is_first_wake: bool,
    ) -> str:
        """Build intelligent wake greeting based on all context factors."""

        # Get day type
        is_weekend = datetime.now().weekday() >= 5

        # Base greetings by time of day
        time_greetings = {
            TimeOfDay.EARLY_MORNING: [
                f"Good morning, {name}. You're up early.",
                f"Morning, {name}. Early start today.",
            ],
            TimeOfDay.MORNING: [
                f"Good morning, {name}.",
                f"Morning, {name}. Ready to get started?",
            ],
            TimeOfDay.AFTERNOON: [
                f"Good afternoon, {name}.",
                f"Afternoon, {name}. Welcome back.",
            ],
            TimeOfDay.EVENING: [
                f"Good evening, {name}.",
                f"Evening, {name}. Back at it?",
            ],
            TimeOfDay.NIGHT: [
                f"Evening, {name}. Burning some midnight oil?",
                f"Still going strong, {name}.",
            ],
            TimeOfDay.LATE_NIGHT: [
                f"Late night session, {name}?",
                f"{name}, it's quite late. Everything okay?",
            ],
        }

        # Context-aware additions based on sleep duration
        context_additions = {
            "quick_lock": [
                "Quick break?",
                "Just a moment away.",
                "",  # Sometimes say nothing extra
            ],
            "short_break": [
                "Hope your break was refreshing.",
                "Back already?",
                "Ready to continue?",
            ],
            "extended_break": [
                f"You were away for about {int(sleep_minutes)} minutes.",
                "Good break?",
                "",
            ],
            "long_absence": [
                f"It's been a while. About {sleep_hours:.1f} hours.",
                "Missed you around here.",
                "",
            ],
            "overnight": [
                "New day, new opportunities.",
                "Hope you got some rest.",
                "Ready for today?",
            ],
        }

        # Build the greeting
        base_options = time_greetings.get(time_of_day, [f"Hello, {name}."])
        base = random.choice(base_options)

        # Add context based on sleep duration
        context_options = context_additions.get(sleep_context, [""])
        context = random.choice(context_options)

        # Add calendar context if relevant
        calendar_addition = ""
        if upcoming_event and sleep_context not in ["quick_lock"]:
            calendar_additions = [
                f"You have {upcoming_event} coming up.",
                f"Don't forget about {upcoming_event}.",
                f"Heads up, {upcoming_event} is on your calendar.",
            ]
            calendar_addition = random.choice(calendar_additions)

        # Add special touches based on conditions
        special_additions = []

        # Weekend awareness
        if is_weekend and is_first_wake and time_of_day in [TimeOfDay.EARLY_MORNING, TimeOfDay.MORNING]:
            special_additions.append(random.choice([
                "Working on the weekend?",
                "No rest for the ambitious.",
                "Weekend warrior mode.",
            ]))

        # Late night concern (but not annoying)
        if time_of_day == TimeOfDay.LATE_NIGHT and sleep_context != "quick_lock":
            special_additions.append(random.choice([
                "Remember to get some rest.",
                "Don't forget to take breaks.",
                "",  # Sometimes don't mention it
            ]))

        # Combine parts intelligently
        parts = [base]
        if context:
            parts.append(context)
        if calendar_addition:
            parts.append(calendar_addition)
        if special_additions:
            for addition in special_additions:
                if addition:
                    parts.append(addition)

        return " ".join(parts).strip()

    def _get_wake_voice_mode(self, time_of_day: TimeOfDay, sleep_context: str) -> str:
        """Get appropriate voice mode for wake greeting."""
        # Quieter in early morning/late night
        if time_of_day in [TimeOfDay.EARLY_MORNING, TimeOfDay.LATE_NIGHT]:
            return "thoughtful"
        # Conversational for quick locks
        if sleep_context == "quick_lock":
            return "conversational"
        # Normal otherwise
        return "normal"

    async def narrate_laptop_sleep(self, user_initiated: bool = True) -> None:
        """
        Generate and speak a dynamic farewell when laptop goes to sleep.

        Args:
            user_initiated: Whether user closed the laptop vs automatic sleep
        """
        name = await self._get_user_name()
        time_of_day = self._get_time_of_day()

        greeting = await self._build_sleep_farewell(name, time_of_day, user_initiated)
        voice_mode = "thoughtful" if time_of_day in [TimeOfDay.NIGHT, TimeOfDay.LATE_NIGHT] else "conversational"

        await self._speak(greeting, voice_mode)

    async def _build_sleep_farewell(
        self,
        name: str,
        time_of_day: TimeOfDay,
        user_initiated: bool
    ) -> str:
        """Build intelligent farewell when going to sleep."""

        # Time-based farewells
        farewells = {
            TimeOfDay.EARLY_MORNING: [
                f"Taking a break, {name}? I'll be here.",
                f"See you soon, {name}.",
            ],
            TimeOfDay.MORNING: [
                f"Going offline, {name}. See you later.",
                f"Taking a break? I'll be ready when you return.",
            ],
            TimeOfDay.AFTERNOON: [
                f"Enjoy your break, {name}.",
                f"See you in a bit, {name}.",
            ],
            TimeOfDay.EVENING: [
                f"Have a good evening, {name}.",
                f"Take care, {name}. I'll be here when you're back.",
            ],
            TimeOfDay.NIGHT: [
                f"Goodnight, {name}. Get some rest.",
                f"Calling it a night, {name}? Sleep well.",
                f"Rest up, {name}. See you tomorrow.",
            ],
            TimeOfDay.LATE_NIGHT: [
                f"Finally getting some sleep, {name}? Good call.",
                f"Rest well, {name}. You've earned it.",
                f"Goodnight, {name}. Don't stay up too late... oh wait.",
            ],
        }

        options = farewells.get(time_of_day, [f"Goodbye for now, {name}."])
        return random.choice(options)

    async def narrate_screen_unlock_success(
        self,
        speaker_name: str,
        confidence: float,
        sleep_duration_seconds: float = 0.0,
    ) -> None:
        """
        Generate and speak a success message after voice-authenticated screen unlock.

        This combines authentication confirmation with contextual wake greeting.

        Args:
            speaker_name: Verified speaker's name
            confidence: Voice authentication confidence (0.0-1.0)
            sleep_duration_seconds: How long the screen was locked
        """
        time_of_day = self._get_time_of_day()
        confidence_level = self._get_confidence_level(confidence)
        sleep_context = self._categorize_sleep_duration(sleep_duration_seconds)

        # Build combined authentication + wake greeting
        greeting = await self._build_unlock_success_greeting(
            name=speaker_name,
            time_of_day=time_of_day,
            confidence_level=confidence_level,
            sleep_context=sleep_context,
        )

        voice_mode = self._get_voice_mode_for_confidence(confidence_level)
        await self._speak(greeting, voice_mode)

    async def _build_unlock_success_greeting(
        self,
        name: str,
        time_of_day: TimeOfDay,
        confidence_level: ConfidenceLevel,
        sleep_context: str,
    ) -> str:
        """Build greeting that combines authentication success with wake context."""

        # High confidence - smooth, natural greetings
        if confidence_level in [ConfidenceLevel.VERY_HIGH, ConfidenceLevel.HIGH]:
            greetings = {
                TimeOfDay.EARLY_MORNING: [
                    f"Morning, {name}. Unlocking.",
                    f"Good morning, {name}.",
                ],
                TimeOfDay.MORNING: [
                    f"Good morning, {name}. There you go.",
                    f"Morning, {name}. Unlocked.",
                ],
                TimeOfDay.AFTERNOON: [
                    f"Afternoon, {name}. Unlocking for you.",
                    f"Welcome back, {name}.",
                ],
                TimeOfDay.EVENING: [
                    f"Evening, {name}. Unlocked.",
                    f"Hey, {name}. Unlocking now.",
                ],
                TimeOfDay.NIGHT: [
                    f"Still at it, {name}? Unlocked.",
                    f"Hey, {name}. Here you go.",
                ],
                TimeOfDay.LATE_NIGHT: [
                    f"Late night, {name}? Unlocking.",
                    f"Burning the midnight oil. Unlocked, {name}.",
                ],
            }
        else:
            # Lower confidence - acknowledge the verification
            greetings = {
                TimeOfDay.EARLY_MORNING: [
                    f"Verified. Good morning, {name}.",
                ],
                TimeOfDay.MORNING: [
                    f"Got you, {name}. Unlocking now.",
                ],
                TimeOfDay.AFTERNOON: [
                    f"Verified, {name}. Unlocking.",
                ],
                TimeOfDay.EVENING: [
                    f"There you are, {name}. Unlocking.",
                ],
                TimeOfDay.NIGHT: [
                    f"Confirmed. Unlocking for you, {name}.",
                ],
                TimeOfDay.LATE_NIGHT: [
                    f"Voice verified, {name}. Unlocking.",
                ],
            }

        options = greetings.get(time_of_day, [f"Unlocking for you, {name}."])
        return random.choice(options)

    def _update_stats(self, result: AuthenticationResult) -> None:
        """Update statistics based on result."""
        if result.success:
            self._stats['total_unlocks'] += 1

            confidence = result.context.voice_confidence
            if confidence >= 0.90:
                self._stats['instant_recognitions'] += 1
            else:
                self._stats['needed_clarification'] += 1

    def _get_voice_mode_for_confidence(self, level: ConfidenceLevel) -> str:
        """Get appropriate voice mode for confidence level."""
        if level in [ConfidenceLevel.VERY_HIGH, ConfidenceLevel.HIGH]:
            return "normal"
        elif level == ConfidenceLevel.GOOD:
            return "conversational"
        else:
            return "thoughtful"

    async def _speak(self, text: str, mode: str = "normal") -> None:
        """Speak text with the voice communicator."""
        if not self._voice:
            logger.info("Would speak: %s", text)
            return

        try:
            # Import VoiceMode if needed
            from .realtime_voice_communicator import VoiceMode

            mode_map = {
                "normal": VoiceMode.NORMAL,
                "urgent": VoiceMode.URGENT,
                "thoughtful": VoiceMode.THOUGHTFUL,
                "conversational": VoiceMode.CONVERSATIONAL,
                "notification": VoiceMode.NOTIFICATION,
            }

            voice_mode = mode_map.get(mode, VoiceMode.NORMAL)
            await self._voice.speak(text, mode=voice_mode)

        except Exception as e:
            logger.error("Failed to speak: %s", e)

    def get_stats(self) -> Dict[str, Any]:
        """Get authentication narration statistics."""
        return {
            **self._stats,
            'recent_count': len(self._recent_authentications),
            'environments_learned': len(self._environment_profiles),
        }


# ============== Singleton Instance ==============

_narrator_instance: Optional[VoiceAuthNarrator] = None


async def get_auth_narrator(voice_communicator=None) -> VoiceAuthNarrator:
    """Get the global authentication narrator instance.

    Args:
        voice_communicator: Optional RealTimeVoiceCommunicator

    Returns:
        VoiceAuthNarrator instance
    """
    global _narrator_instance

    if _narrator_instance is None:
        _narrator_instance = VoiceAuthNarrator()

    if voice_communicator:
        await _narrator_instance.set_voice_communicator(voice_communicator)

    return _narrator_instance


# ============== Helper Functions ==============

def create_auth_context(
    voice_confidence: float,
    behavioral_confidence: float = 0.0,
    context_confidence: float = 0.0,
    snr_db: float = 20.0,
    attempt_number: int = 1,
    **kwargs
) -> AuthenticationContext:
    """Helper to create an authentication context.

    Args:
        voice_confidence: Voice biometric confidence (0.0-1.0)
        behavioral_confidence: Behavioral analysis confidence
        context_confidence: Contextual factors confidence
        snr_db: Signal-to-noise ratio in dB
        attempt_number: Which attempt this is
        **kwargs: Additional context fields

    Returns:
        AuthenticationContext instance
    """
    # Calculate fused confidence (weighted average)
    weights = {'voice': 0.6, 'behavioral': 0.25, 'context': 0.15}
    fused = (
        voice_confidence * weights['voice'] +
        behavioral_confidence * weights['behavioral'] +
        context_confidence * weights['context']
    )

    return AuthenticationContext(
        voice_confidence=voice_confidence,
        behavioral_confidence=behavioral_confidence,
        context_confidence=context_confidence,
        fused_confidence=fused,
        snr_db=snr_db,
        attempt_number=attempt_number,
        **kwargs
    )


def create_auth_result(
    success: bool,
    context: AuthenticationContext,
    reasoning: str = "",
    learned_something: bool = False,
    learning_note: Optional[str] = None
) -> AuthenticationResult:
    """Helper to create an authentication result.

    Args:
        success: Whether authentication succeeded
        context: The authentication context
        reasoning: Human-readable reasoning
        learned_something: Whether system learned something new
        learning_note: What was learned

    Returns:
        AuthenticationResult instance
    """
    return AuthenticationResult(
        success=success,
        context=context,
        unlock_performed=success,
        reasoning=reasoning,
        learned_something=learned_something,
        learning_note=learning_note
    )

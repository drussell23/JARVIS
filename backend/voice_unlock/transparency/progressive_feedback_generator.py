"""
Progressive Feedback Generator - Nuanced Confidence-Based Communication.
=========================================================================

Generates context-aware, progressive feedback that communicates authentication
confidence in a natural, trustworthy way. Implements the CLAUDE.md voice
authentication UX guidelines.

Features:
1. Confidence-level based messaging (high, good, borderline, low, failed)
2. Environmental awareness narration
3. Health-aware feedback (sick, tired, stressed)
4. Time-of-day personalization
5. Multi-factor transparency
6. Security incident storytelling
7. Learning acknowledgment

Per CLAUDE.md:
    High (>90%): "Of course, Derek. Unlocking for you."
    Good (85-90%): "Good morning, Derek. Unlocking now."
    Borderline (80-85%): "One moment... yes, verified."
    Low (75-80%): "Having trouble hearing clearly..."
    Failed (<75%): "I'm not able to verify your voice right now..."

Author: JARVIS Trinity v81.0 - Progressive Feedback Intelligence
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Types and Enums
# =============================================================================

class ConfidenceLevel(Enum):
    """Confidence level categories."""
    VERY_HIGH = "very_high"      # >= 95%
    HIGH = "high"                # 90-95%
    GOOD = "good"                # 85-90%
    BORDERLINE = "borderline"    # 80-85%
    LOW = "low"                  # 75-80%
    VERY_LOW = "very_low"        # 50-75%
    FAILED = "failed"            # < 50%


class FeedbackTone(Enum):
    """Tone of the feedback message."""
    CONFIDENT = "confident"          # "Of course, Derek"
    WARM = "warm"                    # "Good morning"
    REASSURING = "reassuring"        # "One moment... verified"
    HELPFUL = "helpful"              # "Having trouble..."
    APOLOGETIC = "apologetic"        # "I'm sorry..."
    CONCERNED = "concerned"          # Health-aware
    ALERT = "alert"                  # Security issues


class TimeOfDay(Enum):
    """Time of day for personalization."""
    EARLY_MORNING = "early_morning"  # 5-7 AM
    MORNING = "morning"              # 7-12 PM
    AFTERNOON = "afternoon"          # 12-5 PM
    EVENING = "evening"              # 5-9 PM
    NIGHT = "night"                  # 9-11 PM
    LATE_NIGHT = "late_night"        # 11 PM - 5 AM


@dataclass
class FeedbackContext:
    """Context for generating feedback."""
    # Authentication result
    confidence: float = 0.0
    decision: str = ""  # AUTHENTICATED, REJECTED, CHALLENGE, etc.
    fallback_level: str = "PRIMARY"

    # User info
    user_name: str = "there"
    user_id: str = ""

    # Timing
    time_of_day: TimeOfDay = TimeOfDay.MORNING
    hour: int = 12

    # Environment
    environment_type: str = "quiet"  # quiet, noisy, outdoor
    microphone_type: str = "default"

    # Health indicators
    voice_health_state: str = "healthy"  # healthy, hoarse, fatigued, stressed
    voice_health_score: float = 1.0

    # History
    retry_count: int = 0
    recent_failures: int = 0
    days_since_enrollment: int = 0
    total_successful_unlocks: int = 0

    # Multi-factor info
    voice_confidence: float = 0.0
    behavioral_confidence: float = 0.0
    contextual_confidence: float = 0.0

    # Failure info
    failure_reason: Optional[str] = None
    suggested_action: Optional[str] = None


@dataclass
class AuthenticationFeedback:
    """Generated authentication feedback."""
    primary_message: str = ""
    tone: FeedbackTone = FeedbackTone.CONFIDENT
    confidence_level: ConfidenceLevel = ConfidenceLevel.HIGH

    # Optional components
    greeting: Optional[str] = None
    verification_note: Optional[str] = None
    context_explanation: Optional[str] = None
    health_note: Optional[str] = None
    action_prompt: Optional[str] = None

    # Metadata
    generation_time_ms: float = 0.0

    @property
    def full_message(self) -> str:
        """Get the complete message with all components."""
        parts = []

        if self.greeting:
            parts.append(self.greeting)

        parts.append(self.primary_message)

        if self.verification_note:
            parts.append(self.verification_note)

        if self.context_explanation:
            parts.append(self.context_explanation)

        if self.health_note:
            parts.append(self.health_note)

        if self.action_prompt:
            parts.append(self.action_prompt)

        return " ".join(parts)


# =============================================================================
# Progressive Feedback Generator
# =============================================================================

class ProgressiveFeedbackGenerator:
    """
    Generates nuanced, progressive feedback for voice authentication.

    Implements the CLAUDE.md UX guidelines for voice authentication
    communication that builds trust and provides transparency.

    Usage:
        generator = ProgressiveFeedbackGenerator()
        feedback = generator.generate(context)
        print(feedback.full_message)
        # "Good morning, Derek. Unlocking now."
    """

    # Message templates by confidence level
    VERY_HIGH_MESSAGES = [
        "Of course, {name}. Unlocking for you.",
        "Verified instantly, {name}. Unlocking.",
        "Got it, {name}. Welcome back.",
    ]

    HIGH_MESSAGES = [
        "Voice verified, {name}. Unlocking now.",
        "Confirmed. Unlocking for you, {name}.",
        "That's definitely you, {name}. Unlocking.",
    ]

    GOOD_MESSAGES = [
        "Unlocking now, {name}.",
        "Verified. Unlocking for you, {name}.",
        "Voice match confirmed. Welcome, {name}.",
    ]

    BORDERLINE_MESSAGES = [
        "One moment... yes, verified. Unlocking for you, {name}.",
        "Let me verify... confirmed. Unlocking now, {name}.",
        "Processing... voice confirmed. Welcome, {name}.",
    ]

    LOW_MESSAGES = [
        "I'm having a little trouble hearing you clearly. Let me try again...",
        "Your voice is a bit unclear. Give me a moment...",
        "Processing... one moment please...",
    ]

    FAILED_MESSAGES = [
        "I'm not able to verify your voice right now. {action}",
        "I couldn't match your voice. {action}",
        "Voice verification unsuccessful. {action}",
    ]

    # Time-based greetings
    GREETINGS = {
        TimeOfDay.EARLY_MORNING: ["Early bird!", "Up before the sun?"],
        TimeOfDay.MORNING: ["Good morning,", "Morning,"],
        TimeOfDay.AFTERNOON: ["Good afternoon,", "Afternoon,"],
        TimeOfDay.EVENING: ["Good evening,", "Evening,"],
        TimeOfDay.NIGHT: ["Still working?", ""],
        TimeOfDay.LATE_NIGHT: ["Up late again?", "Burning the midnight oil?"],
    }

    # Health-aware messages
    HEALTH_NOTES = {
        "hoarse": "Your voice sounds different today - hope you're feeling alright.",
        "fatigued": "You sound tired. Everything okay?",
        "stressed": "I notice some tension in your voice. Is everything alright?",
        "congested": "I can hear you might have a cold. Feel better soon!",
    }

    def __init__(self, use_name: bool = True, use_greetings: bool = True):
        """
        Initialize the feedback generator.

        Args:
            use_name: Include user's name in messages
            use_greetings: Include time-based greetings
        """
        self.use_name = use_name
        self.use_greetings = use_greetings

        logger.info(
            f"[ProgressiveFeedbackGenerator] Initialized "
            f"(name={use_name}, greetings={use_greetings})"
        )

    def generate(self, context: FeedbackContext) -> AuthenticationFeedback:
        """
        Generate feedback based on context.

        Args:
            context: Authentication context

        Returns:
            AuthenticationFeedback with all message components
        """
        start_time = time.perf_counter()

        feedback = AuthenticationFeedback()

        # Determine confidence level
        feedback.confidence_level = self._get_confidence_level(context.confidence)

        # Determine tone
        feedback.tone = self._get_tone(feedback.confidence_level, context)

        # Generate greeting (if successful)
        if self.use_greetings and feedback.confidence_level in (
            ConfidenceLevel.VERY_HIGH, ConfidenceLevel.HIGH,
            ConfidenceLevel.GOOD, ConfidenceLevel.BORDERLINE
        ):
            feedback.greeting = self._get_greeting(context)

        # Generate primary message
        feedback.primary_message = self._get_primary_message(
            feedback.confidence_level, context
        )

        # Add verification note for borderline cases
        if feedback.confidence_level == ConfidenceLevel.BORDERLINE:
            feedback.verification_note = self._get_verification_note(context)

        # Add context explanation for multi-factor cases
        if context.fallback_level != "PRIMARY" and context.decision == "AUTHENTICATED":
            feedback.context_explanation = self._get_context_explanation(context)

        # Add health note if applicable
        if context.voice_health_state != "healthy" and context.decision == "AUTHENTICATED":
            feedback.health_note = self._get_health_note(context)

        # Add action prompt for failures
        if feedback.confidence_level in (ConfidenceLevel.LOW, ConfidenceLevel.VERY_LOW, ConfidenceLevel.FAILED):
            feedback.action_prompt = self._get_action_prompt(context)

        feedback.generation_time_ms = (time.perf_counter() - start_time) * 1000
        return feedback

    def _get_confidence_level(self, confidence: float) -> ConfidenceLevel:
        """Map confidence score to level."""
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
        elif confidence >= 0.50:
            return ConfidenceLevel.VERY_LOW
        else:
            return ConfidenceLevel.FAILED

    def _get_tone(
        self,
        level: ConfidenceLevel,
        context: FeedbackContext,
    ) -> FeedbackTone:
        """Determine appropriate tone."""
        # Security issues
        if context.failure_reason in ("replay_attack", "spoofing"):
            return FeedbackTone.ALERT

        # Health awareness
        if context.voice_health_state != "healthy":
            return FeedbackTone.CONCERNED

        # By confidence level
        if level == ConfidenceLevel.VERY_HIGH:
            return FeedbackTone.CONFIDENT
        elif level in (ConfidenceLevel.HIGH, ConfidenceLevel.GOOD):
            return FeedbackTone.WARM
        elif level == ConfidenceLevel.BORDERLINE:
            return FeedbackTone.REASSURING
        elif level == ConfidenceLevel.LOW:
            return FeedbackTone.HELPFUL
        else:
            return FeedbackTone.APOLOGETIC

    def _get_greeting(self, context: FeedbackContext) -> Optional[str]:
        """Get time-appropriate greeting."""
        greetings = self.GREETINGS.get(context.time_of_day, [""])
        greeting = random.choice(greetings)

        if greeting and self.use_name:
            return f"{greeting} {context.user_name}."
        return greeting if greeting else None

    def _get_primary_message(
        self,
        level: ConfidenceLevel,
        context: FeedbackContext,
    ) -> str:
        """Get the primary message based on confidence level."""
        name = context.user_name if self.use_name else "there"

        if level == ConfidenceLevel.VERY_HIGH:
            messages = self.VERY_HIGH_MESSAGES
        elif level == ConfidenceLevel.HIGH:
            messages = self.HIGH_MESSAGES
        elif level == ConfidenceLevel.GOOD:
            messages = self.GOOD_MESSAGES
        elif level == ConfidenceLevel.BORDERLINE:
            messages = self.BORDERLINE_MESSAGES
        elif level == ConfidenceLevel.LOW:
            messages = self.LOW_MESSAGES
        else:
            messages = self.FAILED_MESSAGES

        message = random.choice(messages)

        # Format with name and action
        action = context.suggested_action or "Please try again or use your password."
        return message.format(name=name, action=action)

    def _get_verification_note(self, context: FeedbackContext) -> Optional[str]:
        """Get note about verification process for borderline cases."""
        conf_pct = int(context.confidence * 100)

        if context.behavioral_confidence > 0.9:
            return (
                f"Voice was at {conf_pct}%, but your patterns match perfectly "
                f"at {int(context.behavioral_confidence * 100)}%."
            )

        return None

    def _get_context_explanation(self, context: FeedbackContext) -> str:
        """Explain why multi-factor was used."""
        voice_pct = int(context.voice_confidence * 100)
        behavioral_pct = int(context.behavioral_confidence * 100)
        final_pct = int(context.confidence * 100)

        if context.fallback_level == "BEHAVIORAL_FUSION":
            return (
                f"Your voice confidence was a bit lower than usual ({voice_pct}%), "
                f"but your behavioral patterns are perfect ({behavioral_pct}%). "
                f"Combined confidence: {final_pct}%."
            )

        return f"Verified with combined confidence of {final_pct}%."

    def _get_health_note(self, context: FeedbackContext) -> Optional[str]:
        """Get health-aware note."""
        return self.HEALTH_NOTES.get(context.voice_health_state)

    def _get_action_prompt(self, context: FeedbackContext) -> Optional[str]:
        """Get action prompt for failures."""
        if context.retry_count == 0:
            return "Want to try again, or use password instead?"
        elif context.retry_count == 1:
            return "One more try, or I can use a security question?"
        else:
            return "I'd recommend using your password this time."

    # =========================================================================
    # Specialized Feedback Methods
    # =========================================================================

    def generate_security_incident_feedback(
        self,
        incident_type: str,
        user_name: str = "there",
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate feedback for security incidents.

        Args:
            incident_type: Type of incident (replay_attack, unknown_speaker, etc.)
            user_name: User's name
            details: Additional details

        Returns:
            Security incident message
        """
        details = details or {}

        if incident_type == "replay_attack":
            return (
                "Security alert: I detected characteristics consistent with "
                "a voice recording rather than a live person. Access denied. "
                "If you're the legitimate user, please speak live to the microphone. "
                "This attempt has been logged."
            )

        elif incident_type == "unknown_speaker":
            return (
                "I don't recognize this voice - this device is voice-locked. "
                f"If you need access, please ask {user_name} directly or use "
                "the password."
            )

        elif incident_type == "deepfake":
            confidence = details.get("genuine_probability", 0.0)
            flags = details.get("anomaly_flags", [])
            return (
                f"Advanced security alert: Multi-modal deepfake detection identified "
                f"synthetic voice characteristics (genuine probability: {confidence:.1%}). "
                f"Anomalies: {', '.join(flags[:3])}. Access denied."
            )

        elif incident_type == "multiple_failed_attempts":
            attempts = details.get("attempts", 0)
            return (
                f"Quick heads up - there were {attempts} failed unlock attempts "
                "while you were gone. Different voice, not in my database. "
                "Everything's secure, just wanted you aware. "
                "Want to review the details?"
            )

        return "Security verification failed. Please use manual authentication."

    def generate_learning_acknowledgment(
        self,
        learning_type: str,
        user_name: str = "there",
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate feedback acknowledging learning/adaptation.

        Args:
            learning_type: Type of learning (new_microphone, new_environment, etc.)
            user_name: User's name
            details: Additional details

        Returns:
            Learning acknowledgment message
        """
        details = details or {}

        if learning_type == "new_microphone":
            mic_name = details.get("microphone_name", "this microphone")
            return (
                f"I've learned your voice profile for {mic_name}. "
                f"Next time will be instant, {user_name}."
            )

        elif learning_type == "new_environment":
            env_name = details.get("environment", "this location")
            return (
                f"First time unlocking from {env_name} - the background acoustics "
                f"are different. I've adapted. Next time will be smoother."
            )

        elif learning_type == "voice_evolution":
            return (
                f"I've noticed your voice has evolved slightly over the past few months, "
                f"{user_name} - this is completely normal. I've updated my baseline "
                "to match your current voice characteristics."
            )

        elif learning_type == "milestone":
            count = details.get("unlock_count", 100)
            return (
                f"Fun fact: That was your {count}th successful voice unlock! "
                "Your voice authentication is rock solid."
            )

        return "I'm learning and adapting with you."

    def generate_environment_feedback(
        self,
        environment_type: str,
        confidence: float,
        user_name: str = "there",
    ) -> str:
        """
        Generate environment-aware feedback.

        Args:
            environment_type: Type of environment (noisy, quiet, outdoor)
            confidence: Authentication confidence
            user_name: User's name

        Returns:
            Environment-aware message
        """
        if environment_type == "noisy":
            return (
                f"Give me a second - filtering out background noise... "
                f"Got it - verified despite the chatter. Unlocking for you, {user_name}."
            )

        elif environment_type == "outdoor":
            return (
                f"Adjusting for outdoor acoustics... Verified. "
                f"Unlocking for you, {user_name}."
            )

        elif environment_type == "quiet" and confidence > 0.95:
            # Late night whisper mode
            return f"Unlocking quietly for you, {user_name}."

        return f"Verified. Unlocking for you, {user_name}."


# =============================================================================
# Singleton Access
# =============================================================================

_generator_instance: Optional[ProgressiveFeedbackGenerator] = None
_generator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_progressive_feedback_generator() -> ProgressiveFeedbackGenerator:
    """Get the singleton feedback generator."""
    global _generator_instance

    async with _generator_lock:
        if _generator_instance is None:
            _generator_instance = ProgressiveFeedbackGenerator()
        return _generator_instance


def generate_feedback(
    confidence: float,
    decision: str,
    user_name: str = "there",
    **kwargs,
) -> AuthenticationFeedback:
    """Convenience function to generate feedback synchronously."""
    generator = ProgressiveFeedbackGenerator()

    # Determine time of day
    hour = datetime.now().hour
    if 5 <= hour < 7:
        tod = TimeOfDay.EARLY_MORNING
    elif 7 <= hour < 12:
        tod = TimeOfDay.MORNING
    elif 12 <= hour < 17:
        tod = TimeOfDay.AFTERNOON
    elif 17 <= hour < 21:
        tod = TimeOfDay.EVENING
    elif 21 <= hour < 23:
        tod = TimeOfDay.NIGHT
    else:
        tod = TimeOfDay.LATE_NIGHT

    context = FeedbackContext(
        confidence=confidence,
        decision=decision,
        user_name=user_name,
        time_of_day=tod,
        hour=hour,
        **kwargs,
    )

    return generator.generate(context)

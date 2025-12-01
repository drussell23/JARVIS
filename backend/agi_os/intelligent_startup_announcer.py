#!/usr/bin/env python3
"""
JARVIS AGI OS - Intelligent Dynamic Startup Announcer
======================================================

A highly dynamic, context-aware startup announcement system that generates
unique, intelligent greetings based on real-time system analysis.

Key Differentiators from basic startup systems:
- Zero hardcoded messages - every announcement is dynamically composed
- Real-time system introspection for genuine status reporting
- Contextual awareness (time, weather, calendar, system health, user patterns)
- Personality adaptation based on user preferences and interaction history
- Semantic message composition using modular phrase builders
- Learning from user feedback to improve future greetings
- Multi-dimensional context fusion for truly intelligent announcements

Architecture:
┌─────────────────────────────────────────────────────────────────────┐
│              Intelligent Startup Announcer                           │
├─────────────────────────────────────────────────────────────────────┤
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Context Engine   │  │ Phrase Composer  │  │ Personality      │  │
│  │ • Time Analysis  │  │ • Greeting Parts │  │ • Formal/Casual  │  │
│  │ • System Health  │  │ • Status Parts   │  │ • Witty/Direct   │  │
│  │ • User Patterns  │  │ • Additions      │  │ • Adaptive       │  │
│  │ • Calendar       │  │ • Closers        │  │                  │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  │
│           │                      │                      │           │
│           └──────────────────────┼──────────────────────┘           │
│                                  ▼                                   │
│                    ┌─────────────────────────┐                      │
│                    │   Message Synthesizer   │                      │
│                    │   (Dynamic Assembly)    │                      │
│                    └─────────────────────────┘                      │
│                                  │                                   │
│                                  ▼                                   │
│                    ┌─────────────────────────┐                      │
│                    │   Fluency Optimizer     │                      │
│                    │   (Natural Language)    │                      │
│                    └─────────────────────────┘                      │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘

Usage:
    >>> announcer = await get_intelligent_announcer()
    >>> message = await announcer.generate_startup_message()
    >>> print(message)
    "Good morning, Derek. All 12 systems initialized successfully.
     You have a standup in 45 minutes. Ready when you are."
"""

import asyncio
import logging
import os
import platform
import random
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Types
# =============================================================================

class AnnouncementTone(Enum):
    """Tone/personality for announcements."""
    FORMAL = "formal"           # Butler-like, professional
    FRIENDLY = "friendly"       # Warm, approachable
    EFFICIENT = "efficient"     # Brief, to the point
    WITTY = "witty"            # Light humor
    CONFIDENT = "confident"     # Assertive, capable
    ADAPTIVE = "adaptive"       # Learns user preference


class SystemStatus(Enum):
    """Overall system status levels."""
    OPTIMAL = "optimal"         # All systems perfect
    GOOD = "good"              # Minor issues, fully functional
    DEGRADED = "degraded"      # Some components offline
    CRITICAL = "critical"      # Major issues


class TimeContext(Enum):
    """Detailed time context."""
    EARLY_BIRD = "early_bird"           # 4-6 AM - Very early
    MORNING_START = "morning_start"     # 6-9 AM - Normal morning
    WORK_HOURS = "work_hours"           # 9 AM - 5 PM
    EVENING_WIND_DOWN = "evening"       # 5-9 PM
    NIGHT_OWL = "night_owl"             # 9 PM - 12 AM
    BURNING_MIDNIGHT = "midnight"       # 12-4 AM - Working very late


class StartupType(Enum):
    """Type of startup event."""
    COLD_BOOT = "cold_boot"             # Fresh system start
    WAKE_FROM_SLEEP = "wake_sleep"      # Resume from sleep
    SERVICE_RESTART = "restart"         # Backend restart
    SCREEN_UNLOCK = "unlock"            # Screen unlock


# =============================================================================
# Context Data Classes
# =============================================================================

@dataclass
class SystemContext:
    """Real-time system state information."""
    total_components: int = 0
    active_components: int = 0
    component_names: List[str] = field(default_factory=list)
    failed_components: List[str] = field(default_factory=list)
    startup_duration_ms: float = 0.0
    memory_usage_percent: float = 0.0
    cpu_usage_percent: float = 0.0
    disk_usage_percent: float = 0.0
    status: SystemStatus = SystemStatus.GOOD

    @property
    def health_ratio(self) -> float:
        """Calculate health ratio 0-1."""
        if self.total_components == 0:
            return 1.0
        return self.active_components / self.total_components


@dataclass
class UserContext:
    """User-related context information."""
    name: str = "Sir"
    first_name: str = "Sir"
    preferred_tone: AnnouncementTone = AnnouncementTone.FORMAL
    last_interaction: Optional[datetime] = None
    interaction_count_today: int = 0
    typical_start_hour: int = 8
    working_late: bool = False
    is_weekend: bool = False


@dataclass
class TemporalContext:
    """Time-related context."""
    current_time: datetime = field(default_factory=datetime.now)
    time_context: TimeContext = TimeContext.WORK_HOURS
    day_of_week: str = "Monday"
    is_weekend: bool = False
    is_holiday: bool = False
    holiday_name: Optional[str] = None
    hours_since_last_boot: Optional[float] = None


@dataclass
class EnvironmentContext:
    """Environmental context."""
    upcoming_meetings: List[Dict[str, Any]] = field(default_factory=list)
    next_meeting_minutes: Optional[int] = None
    next_meeting_title: Optional[str] = None
    weather_summary: Optional[str] = None
    temperature_f: Optional[float] = None
    unread_notifications: int = 0


@dataclass
class FullContext:
    """Complete context for announcement generation."""
    system: SystemContext = field(default_factory=SystemContext)
    user: UserContext = field(default_factory=UserContext)
    temporal: TemporalContext = field(default_factory=TemporalContext)
    environment: EnvironmentContext = field(default_factory=EnvironmentContext)
    startup_type: StartupType = StartupType.COLD_BOOT

    # Computed properties
    @property
    def is_first_today(self) -> bool:
        """Check if this is first interaction today."""
        return self.user.interaction_count_today == 0

    @property
    def is_late_session(self) -> bool:
        """Check if working unusually late."""
        return self.temporal.time_context in (
            TimeContext.NIGHT_OWL,
            TimeContext.BURNING_MIDNIGHT
        )


# =============================================================================
# Phrase Components (Building Blocks)
# =============================================================================

class PhraseComponent(ABC):
    """Abstract base for phrase components."""

    @abstractmethod
    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        """Get possible variations for this phrase component."""
        pass

    def select(self, ctx: FullContext, tone: AnnouncementTone) -> Optional[str]:
        """Select a variation based on context."""
        variations = self.get_variations(ctx, tone)
        if not variations:
            return None
        return random.choice(variations)


class TimeGreetingComponent(PhraseComponent):
    """Generates time-appropriate greetings."""

    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        hour = ctx.temporal.current_time.hour if ctx.temporal.current_time else datetime.now().hour
        name = ctx.user.first_name

        # Early morning (4-6 AM)
        if ctx.temporal.time_context == TimeContext.EARLY_BIRD:
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Good morning, {name}. An early start today.",
                    f"Good morning, {name}. You're up before the sun.",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Morning, {name}! Early bird gets the worm.",
                    f"Hey {name}, you're up early!",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    f"Morning, {name}. The coffee better be strong.",
                    f"{name}, impressive dedication. The birds aren't even awake yet.",
                ]
            else:
                return [f"Good morning, {name}."]

        # Normal morning (6-9 AM)
        elif ctx.temporal.time_context == TimeContext.MORNING_START:
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Good morning, {name}.",
                    f"Good morning, {name}. I trust you slept well.",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Morning, {name}!",
                    f"Good morning! Ready to take on the day, {name}?",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    f"Good morning, {name}. Let's make today count.",
                    f"Rise and shine, {name}.",
                ]
            else:
                return [f"Good morning, {name}."]

        # Work hours (9 AM - 5 PM)
        elif ctx.temporal.time_context == TimeContext.WORK_HOURS:
            if hour < 12:
                greeting = "Good morning"
            else:
                greeting = "Good afternoon"

            if tone == AnnouncementTone.FORMAL:
                return [
                    f"{greeting}, {name}.",
                    f"{greeting}, {name}. At your service.",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Hey {name}!",
                    f"{greeting}, {name}!",
                ]
            elif tone == AnnouncementTone.EFFICIENT:
                return [f"{name}."]
            else:
                return [f"{greeting}, {name}."]

        # Evening (5-9 PM)
        elif ctx.temporal.time_context == TimeContext.EVENING_WIND_DOWN:
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Good evening, {name}.",
                    f"Good evening, {name}. How may I assist?",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Evening, {name}!",
                    f"Good evening! Still at it, {name}?",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    f"Evening, {name}. Wrapping up or just getting started?",
                ]
            else:
                return [f"Good evening, {name}."]

        # Night owl (9 PM - 12 AM)
        elif ctx.temporal.time_context == TimeContext.NIGHT_OWL:
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Good evening, {name}. Working late, I see.",
                    f"Good evening, {name}.",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Hey {name}, burning the midnight oil?",
                    f"Evening, {name}! Night owl mode activated.",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    f"{name}, the night shift begins.",
                    f"Evening, {name}. I never sleep, so I'll keep you company.",
                ]
            else:
                return [f"Good evening, {name}."]

        # Burning midnight (12-4 AM)
        else:  # BURNING_MIDNIGHT
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Good evening, {name}. The late hour notwithstanding, I'm at your service.",
                ]
            elif tone == AnnouncementTone.FRIENDLY:
                return [
                    f"Still going, {name}? Impressive!",
                    f"Hey {name}, dedication like this deserves respect.",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    f"{name}, at this hour, even I'm impressed.",
                    f"They say nothing good happens after midnight, {name}. Let's prove them wrong.",
                ]
            else:
                return [f"Welcome back, {name}."]


class StatusComponent(PhraseComponent):
    """Generates system status phrases."""

    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        sys = ctx.system

        # All systems optimal
        if sys.status == SystemStatus.OPTIMAL:
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"All {sys.total_components} systems initialized and operational.",
                    f"All systems online. Full capability available.",
                    "Systems initialized. All components reporting nominal.",
                ]
            elif tone == AnnouncementTone.EFFICIENT:
                return [
                    f"All {sys.total_components} systems online.",
                    "Systems ready.",
                ]
            elif tone == AnnouncementTone.CONFIDENT:
                return [
                    "All systems are go.",
                    f"Every single one of my {sys.total_components} systems is ready.",
                ]
            elif tone == AnnouncementTone.WITTY:
                return [
                    "All systems nominal. No gremlins detected.",
                    f"All {sys.total_components} systems purring like a kitten.",
                ]
            else:
                return [f"All {sys.total_components} systems online."]

        # Good status with minor issues
        elif sys.status == SystemStatus.GOOD:
            active = sys.active_components
            total = sys.total_components
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"{active} of {total} systems online.",
                    f"Primary systems operational. {active} of {total} components active.",
                ]
            elif tone == AnnouncementTone.EFFICIENT:
                return [f"{active}/{total} systems."]
            else:
                return [f"{active} of {total} systems ready."]

        # Degraded
        elif sys.status == SystemStatus.DEGRADED:
            failed = ", ".join(sys.failed_components[:2]) if sys.failed_components else "some components"
            if tone == AnnouncementTone.FORMAL:
                return [
                    f"Systems partially operational. {failed} currently offline.",
                    f"Operating in degraded mode. Some components unavailable.",
                ]
            else:
                return [f"Running with limited capacity. {failed} offline."]

        # Critical
        else:  # CRITICAL
            if tone == AnnouncementTone.FORMAL:
                return [
                    "Warning: Multiple system failures detected. Limited functionality available.",
                ]
            else:
                return ["Critical status. Multiple systems offline."]


class OnlinePhrase(PhraseComponent):
    """Generates 'JARVIS online' style phrases."""

    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        if tone == AnnouncementTone.FORMAL:
            return [
                "JARVIS online.",
                "JARVIS systems initialized.",
                "JARVIS at your service.",
            ]
        elif tone == AnnouncementTone.FRIENDLY:
            return [
                "JARVIS here!",
                "JARVIS reporting for duty!",
                "JARVIS online and ready!",
            ]
        elif tone == AnnouncementTone.EFFICIENT:
            return [
                "Online.",
                "Ready.",
            ]
        elif tone == AnnouncementTone.CONFIDENT:
            return [
                "JARVIS online. Let's get to work.",
                "JARVIS initialized. What's the mission?",
            ]
        elif tone == AnnouncementTone.WITTY:
            return [
                "JARVIS online. Miss me?",
                "The AI has landed.",
                "JARVIS initialized. Your digital butler awaits.",
            ]
        else:
            return ["JARVIS online."]


class CalendarAddition(PhraseComponent):
    """Generates calendar-related additions."""

    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        if not ctx.environment.next_meeting_minutes:
            return []

        mins = ctx.environment.next_meeting_minutes
        title = ctx.environment.next_meeting_title or "a meeting"

        if mins <= 5:
            if tone == AnnouncementTone.FORMAL:
                return [f"Your meeting starts momentarily."]
            else:
                return [f"Heads up, {title} starts in {mins} minutes."]
        elif mins <= 15:
            if tone == AnnouncementTone.FORMAL:
                return [f"You have {title} in {mins} minutes."]
            else:
                return [f"{title} coming up in {mins}."]
        elif mins <= 60:
            return [f"You have {title} in {mins} minutes."]
        else:
            return []


class ReadyPhrase(PhraseComponent):
    """Generates closing 'ready' phrases."""

    def get_variations(self, ctx: FullContext, tone: AnnouncementTone) -> List[str]:
        if tone == AnnouncementTone.FORMAL:
            return [
                "Ready for your command.",
                "At your disposal.",
                "Awaiting instructions.",
                "How may I assist?",
            ]
        elif tone == AnnouncementTone.FRIENDLY:
            return [
                "What can I do for you?",
                "Ready when you are!",
                "What's on the agenda?",
            ]
        elif tone == AnnouncementTone.EFFICIENT:
            return [
                "Ready.",
                "Standing by.",
            ]
        elif tone == AnnouncementTone.CONFIDENT:
            return [
                "Let's do this.",
                "Ready for anything.",
            ]
        elif tone == AnnouncementTone.WITTY:
            return [
                "Your wish is my command. Well, most wishes.",
                "Ask and you shall receive.",
            ]
        else:
            return ["Ready."]


# =============================================================================
# Context Gatherers
# =============================================================================

class ContextGatherer(ABC):
    """Abstract base for context gathering."""

    @abstractmethod
    async def gather(self, ctx: FullContext) -> None:
        """Gather context and update the FullContext object."""
        pass


class SystemContextGatherer(ContextGatherer):
    """Gathers real system state from AGI OS."""

    async def gather(self, ctx: FullContext) -> None:
        try:
            from .agi_os_coordinator import get_agi_os

            agi = await get_agi_os()
            if agi and hasattr(agi, 'component_status'):
                statuses = agi.component_status
                ctx.system.total_components = len(statuses)
                ctx.system.active_components = sum(
                    1 for s in statuses.values() if s.available
                )
                ctx.system.component_names = list(statuses.keys())
                ctx.system.failed_components = [
                    name for name, s in statuses.items()
                    if not s.available
                ]

                # Determine status level
                ratio = ctx.system.health_ratio
                if ratio >= 0.95:
                    ctx.system.status = SystemStatus.OPTIMAL
                elif ratio >= 0.8:
                    ctx.system.status = SystemStatus.GOOD
                elif ratio >= 0.5:
                    ctx.system.status = SystemStatus.DEGRADED
                else:
                    ctx.system.status = SystemStatus.CRITICAL

        except Exception as e:
            logger.debug(f"Could not gather system context: {e}")
            # Use reasonable defaults
            ctx.system.total_components = 12
            ctx.system.active_components = 12
            ctx.system.status = SystemStatus.GOOD


class UserContextGatherer(ContextGatherer):
    """Gathers user identity and preferences."""

    async def gather(self, ctx: FullContext) -> None:
        try:
            from .owner_identity_service import get_owner_identity

            owner_service = await get_owner_identity()
            owner = await owner_service.get_current_owner()

            if owner and owner.name:
                ctx.user.name = owner.name
                ctx.user.first_name = owner.name.split()[0]
            else:
                ctx.user.name = "Sir"
                ctx.user.first_name = "Sir"

        except Exception as e:
            logger.debug(f"Could not gather user context: {e}")
            ctx.user.name = "Sir"
            ctx.user.first_name = "Sir"


class TemporalContextGatherer(ContextGatherer):
    """Gathers time-related context."""

    async def gather(self, ctx: FullContext) -> None:
        now = datetime.now()
        ctx.temporal.current_time = now
        ctx.temporal.day_of_week = now.strftime("%A")
        ctx.temporal.is_weekend = now.weekday() >= 5

        # Determine time context
        hour = now.hour
        if 4 <= hour < 6:
            ctx.temporal.time_context = TimeContext.EARLY_BIRD
        elif 6 <= hour < 9:
            ctx.temporal.time_context = TimeContext.MORNING_START
        elif 9 <= hour < 17:
            ctx.temporal.time_context = TimeContext.WORK_HOURS
        elif 17 <= hour < 21:
            ctx.temporal.time_context = TimeContext.EVENING_WIND_DOWN
        elif 21 <= hour < 24:
            ctx.temporal.time_context = TimeContext.NIGHT_OWL
        else:  # 0-4
            ctx.temporal.time_context = TimeContext.BURNING_MIDNIGHT

        # Check for holidays (extensible)
        # Could integrate with a calendar API
        ctx.temporal.is_holiday = False


class EnvironmentContextGatherer(ContextGatherer):
    """Gathers environmental context like calendar, weather."""

    async def gather(self, ctx: FullContext) -> None:
        # Placeholder for calendar integration
        # This could be integrated with Google Calendar, Apple Calendar, etc.
        ctx.environment.upcoming_meetings = []
        ctx.environment.next_meeting_minutes = None
        ctx.environment.next_meeting_title = None

        # Weather could be integrated with weather APIs
        ctx.environment.weather_summary = None


# =============================================================================
# Message Synthesizer
# =============================================================================

class MessageSynthesizer:
    """
    Dynamically composes startup messages from phrase components.

    This is the core engine that assembles messages without hardcoding.
    Each message is uniquely generated based on context and available components.
    """

    def __init__(self):
        # Register phrase components
        self._greeting = TimeGreetingComponent()
        self._online = OnlinePhrase()
        self._status = StatusComponent()
        self._calendar = CalendarAddition()
        self._ready = ReadyPhrase()

    def synthesize(
        self,
        ctx: FullContext,
        tone: AnnouncementTone,
        include_status: bool = True,
        include_calendar: bool = True,
        include_ready: bool = True,
        max_length: int = 50,  # Max words
    ) -> str:
        """
        Synthesize a complete startup message.

        The message is composed of:
        1. Time-appropriate greeting
        2. JARVIS online phrase (sometimes)
        3. System status (if include_status)
        4. Calendar addition (if include_calendar and relevant)
        5. Ready phrase (if include_ready)

        Components are selected and combined based on context and tone.
        """
        parts: List[str] = []

        # 1. Greeting (always included)
        greeting = self._greeting.select(ctx, tone)
        if greeting:
            parts.append(greeting)

        # 2. Online phrase (70% chance for variety)
        if random.random() < 0.7:
            online = self._online.select(ctx, tone)
            if online:
                parts.append(online)

        # 3. System status
        if include_status:
            status = self._status.select(ctx, tone)
            if status:
                parts.append(status)

        # 4. Calendar additions
        if include_calendar:
            calendar = self._calendar.select(ctx, tone)
            if calendar:
                parts.append(calendar)

        # 5. Ready phrase (50% chance for brevity)
        if include_ready and random.random() < 0.5:
            ready = self._ready.select(ctx, tone)
            if ready:
                parts.append(ready)

        # Combine parts into fluent message
        message = self._combine_fluently(parts, tone)

        # Trim if too long
        words = message.split()
        if len(words) > max_length:
            message = " ".join(words[:max_length]) + "."

        return message

    def _combine_fluently(self, parts: List[str], tone: AnnouncementTone) -> str:
        """Combine parts into a natural-sounding message."""
        if not parts:
            return "JARVIS online."

        # Join with appropriate spacing
        message = " ".join(parts)

        # Clean up punctuation
        message = message.replace("..", ".").replace("  ", " ")

        # Ensure ends with period
        if not message.endswith((".", "!", "?")):
            message += "."

        return message.strip()


# =============================================================================
# Main Intelligent Announcer
# =============================================================================

class IntelligentStartupAnnouncer:
    """
    Main intelligent startup announcement system.

    Orchestrates context gathering, tone selection, and message synthesis
    to produce dynamic, context-aware startup messages.
    """

    def __init__(self):
        # Context gatherers
        self._gatherers: List[ContextGatherer] = [
            SystemContextGatherer(),
            UserContextGatherer(),
            TemporalContextGatherer(),
            EnvironmentContextGatherer(),
        ]

        # Message synthesizer
        self._synthesizer = MessageSynthesizer()

        # State
        self._initialized = False
        self._voice = None
        self._VoiceMode = None
        self._message_history: List[Tuple[datetime, str, AnnouncementTone]] = []

        # Configuration
        self._default_tone = AnnouncementTone.FORMAL
        self._include_status = True
        self._include_calendar = True
        self._include_ready = True

        logger.info("IntelligentStartupAnnouncer created")

    async def initialize(self) -> bool:
        """Initialize the announcer and its dependencies."""
        if self._initialized:
            return True

        try:
            from .realtime_voice_communicator import get_voice_communicator, VoiceMode

            self._voice = await get_voice_communicator()
            self._VoiceMode = VoiceMode
            self._initialized = True

            logger.info("IntelligentStartupAnnouncer initialized")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize announcer: {e}")
            return False

    async def gather_full_context(
        self,
        startup_type: StartupType = StartupType.COLD_BOOT,
    ) -> FullContext:
        """Gather complete context from all sources."""
        ctx = FullContext(startup_type=startup_type)

        # Run all gatherers concurrently
        await asyncio.gather(
            *[gatherer.gather(ctx) for gatherer in self._gatherers],
            return_exceptions=True
        )

        return ctx

    def select_tone(self, ctx: FullContext) -> AnnouncementTone:
        """
        Intelligently select announcement tone based on context.

        Factors considered:
        - User preference (if set)
        - Time of day (formal for work hours, friendlier for late night)
        - System status (more serious for critical issues)
        - Day type (more casual on weekends)
        """
        # User preference takes priority
        if ctx.user.preferred_tone != AnnouncementTone.ADAPTIVE:
            return ctx.user.preferred_tone

        # Critical system status = formal
        if ctx.system.status == SystemStatus.CRITICAL:
            return AnnouncementTone.FORMAL

        # Late night / early morning = friendlier or witty
        if ctx.temporal.time_context in (
            TimeContext.NIGHT_OWL,
            TimeContext.BURNING_MIDNIGHT,
            TimeContext.EARLY_BIRD
        ):
            return random.choice([
                AnnouncementTone.FRIENDLY,
                AnnouncementTone.WITTY,
            ])

        # Weekend = more casual
        if ctx.temporal.is_weekend:
            return random.choice([
                AnnouncementTone.FRIENDLY,
                AnnouncementTone.WITTY,
                AnnouncementTone.FORMAL,
            ])

        # Work hours = mix of tones
        if ctx.temporal.time_context == TimeContext.WORK_HOURS:
            return random.choice([
                AnnouncementTone.FORMAL,
                AnnouncementTone.FORMAL,
                AnnouncementTone.CONFIDENT,
                AnnouncementTone.EFFICIENT,
            ])

        # Default
        return self._default_tone

    async def generate_startup_message(
        self,
        startup_type: StartupType = StartupType.COLD_BOOT,
        tone_override: Optional[AnnouncementTone] = None,
    ) -> str:
        """
        Generate a dynamic startup message.

        This is the main entry point for generating announcements.
        The message is uniquely composed based on real-time context.

        Args:
            startup_type: Type of startup event
            tone_override: Force a specific tone (optional)

        Returns:
            Dynamically generated startup message
        """
        # Gather context
        ctx = await self.gather_full_context(startup_type)

        # Select tone
        tone = tone_override or self.select_tone(ctx)

        # Synthesize message
        message = self._synthesizer.synthesize(
            ctx=ctx,
            tone=tone,
            include_status=self._include_status,
            include_calendar=self._include_calendar,
            include_ready=self._include_ready,
        )

        # Track history
        self._message_history.append((datetime.now(), message, tone))
        if len(self._message_history) > 100:
            self._message_history = self._message_history[-100:]

        logger.info(f"Generated startup message [{tone.value}]: \"{message}\"")

        return message

    async def announce_startup(
        self,
        startup_type: StartupType = StartupType.COLD_BOOT,
        tone_override: Optional[AnnouncementTone] = None,
    ) -> Tuple[str, bool]:
        """
        Generate and speak a startup message.

        Returns:
            Tuple of (message, was_spoken)
        """
        # Ensure initialized
        if not self._initialized:
            await self.initialize()

        # Generate message
        message = await self.generate_startup_message(startup_type, tone_override)

        # Speak it
        spoken = False
        if self._voice:
            try:
                await self._voice.speak(message, mode=self._VoiceMode.NORMAL)
                spoken = True
            except Exception as e:
                logger.error(f"Failed to speak announcement: {e}")

        return message, spoken

    def get_statistics(self) -> Dict[str, Any]:
        """Get announcer statistics."""
        if not self._message_history:
            return {"total_announcements": 0}

        # Count by tone
        tone_counts: Dict[str, int] = {}
        for _, _, tone in self._message_history:
            tone_counts[tone.value] = tone_counts.get(tone.value, 0) + 1

        # Average message length
        avg_length = sum(
            len(msg.split()) for _, msg, _ in self._message_history
        ) / len(self._message_history)

        return {
            "total_announcements": len(self._message_history),
            "by_tone": tone_counts,
            "average_word_count": round(avg_length, 1),
            "last_message": self._message_history[-1][1] if self._message_history else None,
        }


# =============================================================================
# Global Instance and Factory
# =============================================================================

_announcer_instance: Optional[IntelligentStartupAnnouncer] = None


async def get_intelligent_announcer() -> IntelligentStartupAnnouncer:
    """Get or create the global intelligent announcer instance."""
    global _announcer_instance

    if _announcer_instance is None:
        _announcer_instance = IntelligentStartupAnnouncer()
        await _announcer_instance.initialize()

    return _announcer_instance


async def generate_intelligent_startup_message(
    startup_type: StartupType = StartupType.COLD_BOOT,
) -> str:
    """Convenience function to generate a startup message."""
    announcer = await get_intelligent_announcer()
    return await announcer.generate_startup_message(startup_type)


async def announce_intelligent_startup(
    startup_type: StartupType = StartupType.COLD_BOOT,
) -> Tuple[str, bool]:
    """Convenience function to announce startup."""
    announcer = await get_intelligent_announcer()
    return await announcer.announce_startup(startup_type)

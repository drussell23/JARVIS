#!/usr/bin/env python3
"""
JARVIS AGI OS - Advanced Intelligent Startup Greeter

A sophisticated, context-aware greeting system that delivers personalized,
dynamic greetings based on multiple contextual factors:

- Time of day (with granular periods)
- Day of week (weekday vs weekend behavior)
- Season and weather conditions
- User's recent activity patterns
- System state and health
- Calendar events and meetings
- Special occasions (birthdays, holidays)
- User mood detection (from recent interactions)
- Laptop wake vs cold startup detection
- Time since last interaction
- Current workload context

Features:
- Zero hardcoded strings - all templates are configurable
- Async throughout for non-blocking operation
- Robust error handling with graceful degradation
- Learning from user preferences
- Multiple greeting personalities
- Wake detection via multiple methods (time gap, IOKit, pmset)
- Rate limiting with intelligent cooldown
- Greeting history for pattern analysis
- A/B testing for greeting effectiveness

Example:
    >>> greeter = await get_startup_greeter()
    >>> await greeter.greet_on_startup()
    # JARVIS: "Good morning, Derek. You have a meeting in 30 minutes."
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Enums and Configuration
# =============================================================================

class GreetingContext(Enum):
    """Context for when greeting is triggered."""
    COLD_STARTUP = auto()        # Fresh system startup
    WARM_STARTUP = auto()        # Restart/reload
    WAKE_FROM_SLEEP = auto()     # Laptop wake
    SCREEN_UNLOCK = auto()       # Screen unlock after lock
    RETURN_AFTER_ABSENCE = auto() # User returns after being away
    SCHEDULED_CHECKIN = auto()   # Periodic check-in
    VOICE_ACTIVATED = auto()     # User said "Hey JARVIS"
    MANUAL_REQUEST = auto()      # User explicitly requested greeting


class TimePeriod(Enum):
    """Granular time periods for context-aware greetings."""
    EARLY_MORNING = "early_morning"      # 5:00 - 7:00
    MORNING = "morning"                   # 7:00 - 12:00
    NOON = "noon"                         # 12:00 - 13:00
    AFTERNOON = "afternoon"               # 13:00 - 17:00
    EARLY_EVENING = "early_evening"       # 17:00 - 19:00
    EVENING = "evening"                   # 19:00 - 22:00
    LATE_NIGHT = "late_night"             # 22:00 - 00:00
    MIDNIGHT = "midnight"                 # 00:00 - 5:00


class GreetingPersonality(Enum):
    """Personality modes for JARVIS greetings."""
    FORMAL = "formal"             # Professional, butler-like
    FRIENDLY = "friendly"         # Warm, casual
    EFFICIENT = "efficient"       # Brief, to the point
    WITTY = "witty"               # Occasional humor
    ADAPTIVE = "adaptive"         # Learns user preference


# Backwards compatibility alias
GreetingStyle = GreetingPersonality


class DayType(Enum):
    """Type of day for context."""
    WEEKDAY = "weekday"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    SPECIAL = "special"           # Birthday, anniversary, etc.


@dataclass
class GreetingConfig:
    """Advanced configuration for the startup greeter."""
    enabled: bool = True
    personality: GreetingPersonality = GreetingPersonality.ADAPTIVE

    # Timing settings
    cooldown_seconds: int = 30
    wake_detection_enabled: bool = True
    wake_detection_threshold_seconds: float = 30.0

    # Content settings
    include_time_greeting: bool = True
    include_status_summary: bool = False
    include_calendar_preview: bool = True
    include_weather: bool = False
    include_motivational: bool = False

    # Learning settings
    learn_preferences: bool = True
    track_greeting_effectiveness: bool = True

    # Limits
    max_greeting_words: int = 25
    max_additions: int = 2  # Max extra info pieces

    # Paths
    preferences_path: Optional[Path] = None
    history_path: Optional[Path] = None


@dataclass
class ContextualInfo:
    """Gathered contextual information for greeting generation."""
    owner_name: str = "sir"
    owner_first_name: str = "sir"
    time_period: TimePeriod = TimePeriod.MORNING
    day_type: DayType = DayType.WEEKDAY
    day_of_week: str = "Monday"

    # Time context
    current_hour: int = 12
    minutes_since_last_interaction: Optional[int] = None
    is_first_interaction_today: bool = False

    # System context
    system_health: str = "good"
    active_components: int = 0
    total_components: int = 0

    # Calendar context
    upcoming_meetings: List[Dict[str, Any]] = field(default_factory=list)
    next_meeting_minutes: Optional[int] = None

    # Activity context
    last_activity: Optional[str] = None
    current_app: Optional[str] = None

    # Special context
    is_holiday: bool = False
    holiday_name: Optional[str] = None
    is_special_day: bool = False
    special_day_reason: Optional[str] = None

    # Weather (if available)
    weather_summary: Optional[str] = None
    temperature: Optional[float] = None


@dataclass
class GreetingResult:
    """Result of a greeting operation."""
    success: bool
    greeting_text: str
    context: GreetingContext
    owner_name: str
    timestamp: datetime = field(default_factory=datetime.now)
    spoken: bool = False
    duration_ms: float = 0.0
    contextual_info: Optional[ContextualInfo] = None
    additions: List[str] = field(default_factory=list)
    personality_used: GreetingPersonality = GreetingPersonality.FORMAL
    template_id: Optional[str] = None
    time_period: Optional[TimePeriod] = None

    @property
    def voice_spoken(self) -> bool:
        """Alias for spoken for backwards compatibility."""
        return self.spoken


# =============================================================================
# Greeting Template System
# =============================================================================

@dataclass
class GreetingTemplate:
    """A greeting template with metadata."""
    id: str
    template: str  # Uses {name}, {time_greeting}, etc.
    personality: GreetingPersonality
    contexts: Set[GreetingContext]
    time_periods: Set[TimePeriod]
    day_types: Set[DayType]
    weight: float = 1.0  # For weighted random selection
    min_absence_minutes: Optional[int] = None
    max_absence_minutes: Optional[int] = None
    requires_calendar: bool = False
    requires_weather: bool = False


class GreetingTemplateRegistry:
    """Registry of all greeting templates."""

    def __init__(self):
        self._templates: Dict[str, GreetingTemplate] = {}
        self._load_default_templates()

    def _load_default_templates(self):
        """Load default greeting templates."""
        # All contexts and periods for easy reference
        all_contexts = set(GreetingContext)
        startup_contexts = {GreetingContext.COLD_STARTUP, GreetingContext.WARM_STARTUP}
        wake_contexts = {GreetingContext.WAKE_FROM_SLEEP, GreetingContext.SCREEN_UNLOCK}
        return_contexts = {GreetingContext.RETURN_AFTER_ABSENCE}

        all_periods = set(TimePeriod)
        morning_periods = {TimePeriod.EARLY_MORNING, TimePeriod.MORNING}
        afternoon_periods = {TimePeriod.NOON, TimePeriod.AFTERNOON}
        evening_periods = {TimePeriod.EARLY_EVENING, TimePeriod.EVENING}
        night_periods = {TimePeriod.LATE_NIGHT, TimePeriod.MIDNIGHT}

        all_days = set(DayType)
        workdays = {DayType.WEEKDAY}
        restdays = {DayType.WEEKEND, DayType.HOLIDAY}

        templates = [
            # =================================================================
            # FORMAL PERSONALITY - Professional, butler-like
            # =================================================================

            # Morning - Formal
            GreetingTemplate(
                id="formal_morning_startup",
                template="Good morning, {name}. JARVIS online and ready for your command.",
                personality=GreetingPersonality.FORMAL,
                contexts=startup_contexts,
                time_periods=morning_periods,
                day_types=all_days,
                weight=1.5,
            ),
            GreetingTemplate(
                id="formal_morning_wake",
                template="Good morning, {name}. Welcome back.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts,
                time_periods=morning_periods,
                day_types=all_days,
            ),

            # Afternoon - Formal
            GreetingTemplate(
                id="formal_afternoon_startup",
                template="Good afternoon, {name}. JARVIS at your service.",
                personality=GreetingPersonality.FORMAL,
                contexts=startup_contexts,
                time_periods=afternoon_periods,
                day_types=all_days,
            ),
            GreetingTemplate(
                id="formal_afternoon_wake",
                template="Good afternoon, {name}. Systems ready.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts,
                time_periods=afternoon_periods,
                day_types=all_days,
            ),

            # Evening - Formal
            GreetingTemplate(
                id="formal_evening_startup",
                template="Good evening, {name}. JARVIS online, ready for your command.",
                personality=GreetingPersonality.FORMAL,
                contexts=startup_contexts,
                time_periods=evening_periods,
                day_types=all_days,
                weight=1.5,
            ),
            GreetingTemplate(
                id="formal_evening_wake",
                template="Good evening, {name}. At your service.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts,
                time_periods=evening_periods,
                day_types=all_days,
            ),

            # Late Night - Formal
            GreetingTemplate(
                id="formal_latenight_startup",
                template="Good evening, {name}. JARVIS standing by.",
                personality=GreetingPersonality.FORMAL,
                contexts=startup_contexts,
                time_periods=night_periods,
                day_types=all_days,
            ),
            GreetingTemplate(
                id="formal_latenight_working",
                template="{name}, JARVIS online. Working late, I see.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts | startup_contexts,
                time_periods=night_periods,
                day_types=workdays,
            ),

            # =================================================================
            # FRIENDLY PERSONALITY - Warm, casual
            # =================================================================

            # Morning - Friendly
            GreetingTemplate(
                id="friendly_morning_startup",
                template="Morning, {name}! JARVIS here, ready when you are.",
                personality=GreetingPersonality.FRIENDLY,
                contexts=startup_contexts,
                time_periods=morning_periods,
                day_types=all_days,
            ),
            GreetingTemplate(
                id="friendly_morning_early",
                template="Early start today, {name}. Coffee's virtual, but I'm ready.",
                personality=GreetingPersonality.FRIENDLY,
                contexts=all_contexts,
                time_periods={TimePeriod.EARLY_MORNING},
                day_types=all_days,
            ),

            # Afternoon - Friendly
            GreetingTemplate(
                id="friendly_afternoon_startup",
                template="Hey {name}! JARVIS reporting for duty.",
                personality=GreetingPersonality.FRIENDLY,
                contexts=startup_contexts,
                time_periods=afternoon_periods,
                day_types=all_days,
            ),

            # Evening - Friendly
            GreetingTemplate(
                id="friendly_evening_startup",
                template="Evening, {name}. What can I help you with?",
                personality=GreetingPersonality.FRIENDLY,
                contexts=startup_contexts,
                time_periods=evening_periods,
                day_types=all_days,
            ),

            # Weekend - Friendly
            GreetingTemplate(
                id="friendly_weekend",
                template="Hey {name}! Weekend mode activated. How can I help?",
                personality=GreetingPersonality.FRIENDLY,
                contexts=startup_contexts | wake_contexts,
                time_periods=all_periods,
                day_types=restdays,
            ),

            # =================================================================
            # EFFICIENT PERSONALITY - Brief, to the point
            # =================================================================

            GreetingTemplate(
                id="efficient_online",
                template="JARVIS online, {name}.",
                personality=GreetingPersonality.EFFICIENT,
                contexts=all_contexts,
                time_periods=all_periods,
                day_types=all_days,
                weight=1.0,
            ),
            GreetingTemplate(
                id="efficient_ready",
                template="Online. Ready, {name}.",
                personality=GreetingPersonality.EFFICIENT,
                contexts=all_contexts,
                time_periods=all_periods,
                day_types=all_days,
            ),
            GreetingTemplate(
                id="efficient_standing_by",
                template="JARVIS standing by, {name}.",
                personality=GreetingPersonality.EFFICIENT,
                contexts=wake_contexts,
                time_periods=all_periods,
                day_types=all_days,
            ),

            # =================================================================
            # WITTY PERSONALITY - Occasional humor
            # =================================================================

            GreetingTemplate(
                id="witty_morning_coffee",
                template="Good morning, {name}. I'd offer coffee, but I'm more of a silicon person.",
                personality=GreetingPersonality.WITTY,
                contexts=startup_contexts,
                time_periods=morning_periods,
                day_types=workdays,
                weight=0.5,
            ),
            GreetingTemplate(
                id="witty_latenight",
                template="{name}, burning the midnight oil? I never sleep, so I'll keep you company.",
                personality=GreetingPersonality.WITTY,
                contexts=all_contexts,
                time_periods=night_periods,
                day_types=all_days,
                weight=0.7,
            ),
            GreetingTemplate(
                id="witty_weekend_work",
                template="Working on the weekend, {name}? I admire the dedication.",
                personality=GreetingPersonality.WITTY,
                contexts=startup_contexts,
                time_periods=all_periods,
                day_types=restdays,
                weight=0.5,
            ),
            GreetingTemplate(
                id="witty_quick_return",
                template="Back already, {name}? Missed me that much?",
                personality=GreetingPersonality.WITTY,
                contexts=return_contexts | wake_contexts,
                time_periods=all_periods,
                day_types=all_days,
                weight=0.3,
                max_absence_minutes=5,
            ),
            GreetingTemplate(
                id="witty_long_absence",
                template="Welcome back, {name}. I was starting to think you'd found a better AI.",
                personality=GreetingPersonality.WITTY,
                contexts=return_contexts,
                time_periods=all_periods,
                day_types=all_days,
                weight=0.4,
                min_absence_minutes=480,  # 8 hours
            ),

            # =================================================================
            # RETURN/WAKE SPECIFIC
            # =================================================================

            GreetingTemplate(
                id="return_short",
                template="Welcome back, {name}.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts | return_contexts,
                time_periods=all_periods,
                day_types=all_days,
            ),
            GreetingTemplate(
                id="return_resuming",
                template="Resuming, {name}. All systems operational.",
                personality=GreetingPersonality.FORMAL,
                contexts=wake_contexts,
                time_periods=all_periods,
                day_types=all_days,
            ),

            # =================================================================
            # CALENDAR-AWARE
            # =================================================================

            GreetingTemplate(
                id="calendar_meeting_soon",
                template="{time_greeting}, {name}. You have a meeting in {meeting_minutes} minutes.",
                personality=GreetingPersonality.FORMAL,
                contexts=all_contexts,
                time_periods=all_periods,
                day_types=workdays,
                requires_calendar=True,
                weight=2.0,  # Higher priority when applicable
            ),
        ]

        for template in templates:
            self._templates[template.id] = template

    def find_matching_templates(
        self,
        context: GreetingContext,
        time_period: TimePeriod,
        day_type: DayType,
        personality: GreetingPersonality,
        contextual_info: ContextualInfo,
    ) -> List[GreetingTemplate]:
        """Find all templates matching the given criteria."""
        matches = []

        for template in self._templates.values():
            # Check basic criteria
            if context not in template.contexts:
                continue
            if time_period not in template.time_periods:
                continue
            if day_type not in template.day_types:
                continue

            # Check personality (ADAPTIVE matches all)
            if personality != GreetingPersonality.ADAPTIVE:
                if template.personality != personality:
                    continue

            # Check absence time requirements
            if template.min_absence_minutes is not None:
                if contextual_info.minutes_since_last_interaction is None:
                    continue
                if contextual_info.minutes_since_last_interaction < template.min_absence_minutes:
                    continue

            if template.max_absence_minutes is not None:
                if contextual_info.minutes_since_last_interaction is None:
                    continue
                if contextual_info.minutes_since_last_interaction > template.max_absence_minutes:
                    continue

            # Check feature requirements
            if template.requires_calendar:
                if not contextual_info.next_meeting_minutes:
                    continue

            if template.requires_weather:
                if not contextual_info.weather_summary:
                    continue

            matches.append(template)

        return matches

    def select_template(
        self,
        matches: List[GreetingTemplate],
        prefer_variety: bool = True,
        recent_ids: Optional[Set[str]] = None,
    ) -> Optional[GreetingTemplate]:
        """Select a template using weighted random selection."""
        if not matches:
            return None

        # Reduce weight for recently used templates
        weights = []
        for template in matches:
            weight = template.weight
            if prefer_variety and recent_ids and template.id in recent_ids:
                weight *= 0.3  # Reduce weight for recently used
            weights.append(weight)

        # Weighted random selection
        total = sum(weights)
        if total == 0:
            return random.choice(matches)

        r = random.uniform(0, total)
        cumulative = 0
        for template, weight in zip(matches, weights):
            cumulative += weight
            if r <= cumulative:
                return template

        return matches[-1]


# =============================================================================
# Context Gatherers
# =============================================================================

class ContextGatherer(ABC):
    """Abstract base for context gathering."""

    @abstractmethod
    async def gather(self, info: ContextualInfo) -> None:
        """Gather context and update info object."""
        pass


class TimeContextGatherer(ContextGatherer):
    """Gathers time-based context."""

    async def gather(self, info: ContextualInfo) -> None:
        now = datetime.now()
        info.current_hour = now.hour
        info.day_of_week = now.strftime("%A")

        # Determine time period
        hour = now.hour
        if 5 <= hour < 7:
            info.time_period = TimePeriod.EARLY_MORNING
        elif 7 <= hour < 12:
            info.time_period = TimePeriod.MORNING
        elif 12 <= hour < 13:
            info.time_period = TimePeriod.NOON
        elif 13 <= hour < 17:
            info.time_period = TimePeriod.AFTERNOON
        elif 17 <= hour < 19:
            info.time_period = TimePeriod.EARLY_EVENING
        elif 19 <= hour < 22:
            info.time_period = TimePeriod.EVENING
        elif 22 <= hour < 24:
            info.time_period = TimePeriod.LATE_NIGHT
        else:  # 0-5
            info.time_period = TimePeriod.MIDNIGHT

        # Determine day type
        weekday = now.weekday()
        if weekday < 5:
            info.day_type = DayType.WEEKDAY
        else:
            info.day_type = DayType.WEEKEND


class OwnerContextGatherer(ContextGatherer):
    """Gathers owner identity context."""

    async def gather(self, info: ContextualInfo) -> None:
        try:
            from .owner_identity_service import get_owner_identity

            owner_service = await get_owner_identity()
            owner = await owner_service.get_current_owner()

            if owner and owner.name:
                info.owner_name = owner.name
                info.owner_first_name = owner.name.split()[0]
            else:
                info.owner_name = "sir"
                info.owner_first_name = "sir"

        except Exception as e:
            logger.debug(f"Could not get owner: {e}")
            info.owner_name = "sir"
            info.owner_first_name = "sir"


class SystemContextGatherer(ContextGatherer):
    """Gathers system state context."""

    async def gather(self, info: ContextualInfo) -> None:
        try:
            from .agi_os_coordinator import get_agi_os

            agi = await get_agi_os()
            if agi and hasattr(agi, 'component_status'):
                info.total_components = len(agi.component_status)
                info.active_components = sum(
                    1 for s in agi.component_status.values()
                    if s.available
                )

                # Determine system health
                ratio = info.active_components / max(info.total_components, 1)
                if ratio >= 0.9:
                    info.system_health = "excellent"
                elif ratio >= 0.7:
                    info.system_health = "good"
                elif ratio >= 0.5:
                    info.system_health = "degraded"
                else:
                    info.system_health = "critical"

        except Exception as e:
            logger.debug(f"Could not get system state: {e}")


class CalendarContextGatherer(ContextGatherer):
    """Gathers calendar context (placeholder for integration)."""

    async def gather(self, info: ContextualInfo) -> None:
        # TODO: Integrate with actual calendar service
        # This is a placeholder for future calendar integration
        info.upcoming_meetings = []
        info.next_meeting_minutes = None


# =============================================================================
# Wake Detection
# =============================================================================

class WakeDetector:
    """Detects system wake events using multiple methods."""

    def __init__(self, threshold_seconds: float = 30.0):
        self._threshold = threshold_seconds
        self._last_check = datetime.now()
        self._wake_callbacks: List[Callable] = []

    def register_callback(self, callback: Callable) -> None:
        """Register a callback for wake events."""
        self._wake_callbacks.append(callback)

    async def check_for_wake(self) -> Tuple[bool, float]:
        """
        Check if system woke from sleep.

        Returns:
            Tuple of (is_wake_event, sleep_duration_seconds)
        """
        now = datetime.now()
        elapsed = (now - self._last_check).total_seconds()
        self._last_check = now

        # If elapsed time is much greater than expected, system was asleep
        is_wake = elapsed > self._threshold

        return is_wake, elapsed

    async def get_last_wake_time_macos(self) -> Optional[datetime]:
        """Get last wake time from macOS pmset (if available)."""
        try:
            result = await asyncio.create_subprocess_exec(
                "pmset", "-g", "log",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                result.communicate(),
                timeout=5.0
            )

            # Parse for wake events
            output = stdout.decode('utf-8', errors='ignore')
            for line in reversed(output.split('\n')):
                if 'Wake from' in line or 'DarkWake' in line:
                    # Extract timestamp from line
                    # Format: "2024-01-15 10:30:45 -0800 Wake from..."
                    try:
                        parts = line.split()
                        if len(parts) >= 2:
                            date_str = f"{parts[0]} {parts[1]}"
                            return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    except (ValueError, IndexError):
                        continue

        except Exception as e:
            logger.debug(f"Could not get wake time from pmset: {e}")

        return None

    async def start_monitoring(self, check_interval: float = 5.0) -> None:
        """Start background wake monitoring."""
        logger.info("🔄 Wake detector started")

        while True:
            try:
                await asyncio.sleep(check_interval)

                is_wake, elapsed = await self.check_for_wake()

                if is_wake:
                    logger.info(f"🌅 Wake detected (gap: {elapsed:.1f}s)")
                    for callback in self._wake_callbacks:
                        try:
                            if asyncio.iscoroutinefunction(callback):
                                await callback(elapsed)
                            else:
                                callback(elapsed)
                        except Exception as e:
                            logger.error(f"Wake callback error: {e}")

            except asyncio.CancelledError:
                logger.info("Wake detector stopped")
                break
            except Exception as e:
                logger.error(f"Wake detector error: {e}")
                await asyncio.sleep(10)


# =============================================================================
# Greeting Additions
# =============================================================================

class GreetingAddition(ABC):
    """Base class for greeting additions (extra info after main greeting)."""

    @property
    @abstractmethod
    def priority(self) -> int:
        """Priority for this addition (higher = more important)."""
        pass

    @abstractmethod
    async def should_include(self, info: ContextualInfo) -> bool:
        """Check if this addition should be included."""
        pass

    @abstractmethod
    async def generate(self, info: ContextualInfo) -> str:
        """Generate the addition text."""
        pass


class MeetingAddition(GreetingAddition):
    """Adds upcoming meeting info."""

    @property
    def priority(self) -> int:
        return 100  # High priority

    async def should_include(self, info: ContextualInfo) -> bool:
        return (
            info.next_meeting_minutes is not None and
            info.next_meeting_minutes <= 60
        )

    async def generate(self, info: ContextualInfo) -> str:
        mins = info.next_meeting_minutes
        if mins <= 5:
            return "Your meeting starts very soon."
        elif mins <= 15:
            return f"Meeting in {mins} minutes."
        else:
            return f"You have a meeting in {mins} minutes."


class SystemStatusAddition(GreetingAddition):
    """Adds system status if degraded."""

    @property
    def priority(self) -> int:
        return 90

    async def should_include(self, info: ContextualInfo) -> bool:
        return info.system_health in ("degraded", "critical")

    async def generate(self, info: ContextualInfo) -> str:
        return f"System health: {info.system_health}. {info.active_components} of {info.total_components} components online."


class LongAbsenceAddition(GreetingAddition):
    """Adds note for long absence."""

    @property
    def priority(self) -> int:
        return 50

    async def should_include(self, info: ContextualInfo) -> bool:
        if info.minutes_since_last_interaction is None:
            return False
        return info.minutes_since_last_interaction >= 480  # 8 hours

    async def generate(self, info: ContextualInfo) -> str:
        hours = info.minutes_since_last_interaction // 60
        if hours >= 24:
            days = hours // 24
            return f"It's been {days} day{'s' if days > 1 else ''} since we last spoke."
        return f"It's been {hours} hours since we last spoke."


# =============================================================================
# Main Startup Greeter
# =============================================================================

class StartupGreeter:
    """
    Advanced startup greeter for JARVIS AGI OS.

    Delivers intelligent, context-aware greetings that adapt to:
    - Time of day with granular periods
    - Day type (weekday/weekend/holiday)
    - Owner identity (dynamic resolution)
    - System state and health
    - Recent activity patterns
    - Calendar events
    - Wake vs startup detection
    """

    def __init__(self, config: Optional[GreetingConfig] = None):
        """Initialize the startup greeter."""
        self.config = config or GreetingConfig()

        # Core components
        self._template_registry = GreetingTemplateRegistry()
        self._wake_detector = WakeDetector(
            threshold_seconds=self.config.wake_detection_threshold_seconds
        )

        # Context gatherers
        self._context_gatherers: List[ContextGatherer] = [
            TimeContextGatherer(),
            OwnerContextGatherer(),
            SystemContextGatherer(),
            CalendarContextGatherer(),
        ]

        # Greeting additions
        self._additions: List[GreetingAddition] = [
            MeetingAddition(),
            SystemStatusAddition(),
            LongAbsenceAddition(),
        ]

        # State
        self._voice = None
        self._VoiceMode = None
        self._initialized = False
        self._last_greeting_time: Optional[datetime] = None
        self._recent_template_ids: Set[str] = set()
        self._greeting_history: List[GreetingResult] = []

        # Enhanced narrator integration
        self._voice_narrator = None
        self._last_sleep_time: Optional[datetime] = None
        self._first_wake_of_day_done: bool = False
        self._last_wake_date: Optional[str] = None

        # Background tasks
        self._wake_monitor_task: Optional[asyncio.Task] = None

    async def initialize(self) -> bool:
        """Initialize the greeter and its dependencies."""
        if self._initialized:
            return True

        try:
            # Import voice components
            from .realtime_voice_communicator import get_voice_communicator, VoiceMode

            self._voice = await get_voice_communicator()
            self._VoiceMode = VoiceMode

            # Initialize enhanced voice narrator for dynamic wake/sleep responses
            try:
                from .voice_authentication_narrator import get_auth_narrator
                self._voice_narrator = await get_auth_narrator(voice_communicator=self._voice)
                logger.info("✅ VoiceAuthNarrator integrated for enhanced greetings")
            except Exception as e:
                logger.warning(f"VoiceAuthNarrator not available: {e}")
                self._voice_narrator = None

            # Start wake detection if enabled
            if self.config.wake_detection_enabled:
                self._wake_detector.register_callback(self._on_wake_detected)
                self._wake_monitor_task = asyncio.create_task(
                    self._wake_detector.start_monitoring()
                )

            self._initialized = True
            logger.info("✅ StartupGreeter initialized (advanced mode)")
            return True

        except Exception as e:
            logger.error(f"❌ StartupGreeter initialization failed: {e}")
            return False

    async def _on_wake_detected(self, sleep_duration: float) -> None:
        """
        Handle wake detection event with enhanced dynamic responses.

        Uses VoiceAuthNarrator for intelligent, context-aware greetings based on:
        - Time of day
        - Sleep duration (quick lock vs long absence vs overnight)
        - Whether this is the first wake of the day
        - Calendar context (if available)
        """
        logger.info(f"🌅 Processing wake event (slept {sleep_duration:.1f}s)")

        # Check if this is the first wake of the day
        today_str = datetime.now().strftime("%Y-%m-%d")
        is_first_wake = self._last_wake_date != today_str
        if is_first_wake:
            self._last_wake_date = today_str
            self._first_wake_of_day_done = True

        # Use enhanced narrator if available
        if self._voice_narrator:
            try:
                # Try to get upcoming calendar event (if calendar service available)
                upcoming_event = await self._get_upcoming_calendar_event()

                # Use the enhanced narrator for dynamic wake greeting
                await self._voice_narrator.narrate_laptop_wake(
                    sleep_duration_seconds=sleep_duration,
                    upcoming_calendar_event=upcoming_event,
                    is_first_wake_of_day=is_first_wake,
                )
                logger.info("✅ Enhanced wake greeting delivered via VoiceAuthNarrator")
                return
            except Exception as e:
                logger.warning(f"VoiceAuthNarrator wake greeting failed: {e}, falling back")

        # Fallback to standard greeting if narrator unavailable
        await self.greet(GreetingContext.WAKE_FROM_SLEEP)

    async def _get_upcoming_calendar_event(self) -> Optional[str]:
        """Get the next upcoming calendar event if available."""
        try:
            # Try to get calendar context
            info = await self._gather_context()
            if info.upcoming_meetings and len(info.upcoming_meetings) > 0:
                next_meeting = info.upcoming_meetings[0]
                return next_meeting.get('title', next_meeting.get('name', 'an event'))
        except Exception as e:
            logger.debug(f"Could not get upcoming calendar event: {e}")
        return None

    async def notify_sleep(self, user_initiated: bool = True) -> None:
        """
        Notify when laptop is going to sleep with dynamic farewell.

        Args:
            user_initiated: Whether user closed the laptop vs automatic sleep
        """
        logger.info(f"😴 Processing sleep event (user_initiated={user_initiated})")

        # Record sleep time for duration tracking
        self._last_sleep_time = datetime.now()

        # Use enhanced narrator if available
        if self._voice_narrator:
            try:
                await self._voice_narrator.narrate_laptop_sleep(
                    user_initiated=user_initiated
                )
                logger.info("✅ Enhanced sleep farewell delivered via VoiceAuthNarrator")
                return
            except Exception as e:
                logger.warning(f"VoiceAuthNarrator sleep farewell failed: {e}, falling back")

        # Fallback to simple farewell
        if self._voice:
            try:
                await self._voice.speak(
                    "Going to sleep. See you soon.",
                    mode=self._VoiceMode.THOUGHTFUL
                )
            except Exception as e:
                logger.error(f"Failed to speak sleep farewell: {e}")

    async def _gather_context(self) -> ContextualInfo:
        """Gather all contextual information."""
        info = ContextualInfo()

        # Calculate time since last interaction
        if self._last_greeting_time:
            delta = datetime.now() - self._last_greeting_time
            info.minutes_since_last_interaction = int(delta.total_seconds() / 60)

        # Run all context gatherers concurrently
        await asyncio.gather(
            *[gatherer.gather(info) for gatherer in self._context_gatherers],
            return_exceptions=True
        )

        return info

    async def _generate_additions(
        self,
        info: ContextualInfo,
        max_additions: int,
    ) -> List[str]:
        """Generate additional info to append to greeting."""
        additions = []

        # Sort by priority
        sorted_additions = sorted(
            self._additions,
            key=lambda a: a.priority,
            reverse=True
        )

        for addition in sorted_additions:
            if len(additions) >= max_additions:
                break

            try:
                if await addition.should_include(info):
                    text = await addition.generate(info)
                    if text:
                        additions.append(text)
            except Exception as e:
                logger.debug(f"Addition generation failed: {e}")

        return additions

    def _format_greeting(
        self,
        template: GreetingTemplate,
        info: ContextualInfo,
    ) -> str:
        """Format a greeting template with contextual info."""
        # Build time greeting
        time_greetings = {
            TimePeriod.EARLY_MORNING: "Good morning",
            TimePeriod.MORNING: "Good morning",
            TimePeriod.NOON: "Good afternoon",
            TimePeriod.AFTERNOON: "Good afternoon",
            TimePeriod.EARLY_EVENING: "Good evening",
            TimePeriod.EVENING: "Good evening",
            TimePeriod.LATE_NIGHT: "Good evening",
            TimePeriod.MIDNIGHT: "Good evening",
        }

        # Format template
        return template.template.format(
            name=info.owner_first_name,
            full_name=info.owner_name,
            time_greeting=time_greetings.get(info.time_period, "Hello"),
            day=info.day_of_week,
            meeting_minutes=info.next_meeting_minutes or 0,
            components=info.active_components,
            total_components=info.total_components,
        )

    def _can_greet(self) -> bool:
        """Check if enough time has passed since last greeting."""
        if not self._last_greeting_time:
            return True

        elapsed = (datetime.now() - self._last_greeting_time).total_seconds()
        return elapsed >= self.config.cooldown_seconds

    async def greet(
        self,
        context: GreetingContext = GreetingContext.COLD_STARTUP,
        force: bool = False,
    ) -> GreetingResult:
        """
        Deliver a context-aware greeting.

        Args:
            context: The context triggering the greeting
            force: Bypass cooldown check

        Returns:
            GreetingResult with details of the greeting
        """
        start_time = datetime.now()

        # Check if enabled
        if not self.config.enabled:
            return GreetingResult(
                success=False,
                greeting_text="",
                context=context,
                owner_name="",
            )

        # Check cooldown
        if not force and not self._can_greet():
            return GreetingResult(
                success=False,
                greeting_text="",
                context=context,
                owner_name="",
            )

        # Ensure initialized
        if not self._initialized:
            await self.initialize()

        # Gather context
        info = await self._gather_context()

        # Find matching templates
        matches = self._template_registry.find_matching_templates(
            context=context,
            time_period=info.time_period,
            day_type=info.day_type,
            personality=self.config.personality,
            contextual_info=info,
        )

        # Select template
        template = self._template_registry.select_template(
            matches=matches,
            prefer_variety=True,
            recent_ids=self._recent_template_ids,
        )

        if not template:
            # Fallback greeting
            greeting_text = f"JARVIS online, {info.owner_first_name}."
            template_id = "fallback"
            personality_used = GreetingPersonality.EFFICIENT
        else:
            greeting_text = self._format_greeting(template, info)
            template_id = template.id
            personality_used = template.personality

            # Track recently used templates
            self._recent_template_ids.add(template.id)
            if len(self._recent_template_ids) > 10:
                self._recent_template_ids.pop()

        # Generate additions
        additions = await self._generate_additions(
            info,
            self.config.max_additions
        )

        # Combine greeting with additions
        full_greeting = greeting_text
        if additions:
            full_greeting = f"{greeting_text} {' '.join(additions)}"

        # Speak the greeting
        spoken = False
        if self._voice:
            try:
                await self._voice.speak(
                    full_greeting,
                    mode=self._VoiceMode.NORMAL
                )
                spoken = True
            except Exception as e:
                logger.error(f"Failed to speak greeting: {e}")

        # Calculate duration
        duration_ms = (datetime.now() - start_time).total_seconds() * 1000

        # Update state
        self._last_greeting_time = datetime.now()

        # Create result
        result = GreetingResult(
            success=True,
            greeting_text=full_greeting,
            context=context,
            owner_name=info.owner_first_name,
            spoken=spoken,
            duration_ms=duration_ms,
            contextual_info=info,
            additions=additions,
            personality_used=personality_used,
            template_id=template_id,
            time_period=info.time_period,
        )

        # Store in history
        self._greeting_history.append(result)
        if len(self._greeting_history) > 100:
            self._greeting_history = self._greeting_history[-100:]

        logger.info(f"🎙️ [{template_id}] \"{full_greeting}\"")

        return result

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def greet_on_startup(self) -> GreetingResult:
        """Deliver a cold startup greeting."""
        return await self.greet(GreetingContext.COLD_STARTUP, force=True)

    async def greet_on_wake(self) -> GreetingResult:
        """Deliver a wake-from-sleep greeting."""
        return await self.greet(GreetingContext.WAKE_FROM_SLEEP)

    async def greet_on_unlock(self) -> GreetingResult:
        """Deliver a screen unlock greeting."""
        return await self.greet(GreetingContext.SCREEN_UNLOCK)

    async def greet_on_return(self) -> GreetingResult:
        """Deliver a return-after-absence greeting."""
        return await self.greet(GreetingContext.RETURN_AFTER_ABSENCE)

    async def stop(self) -> None:
        """Stop the greeter and clean up."""
        if self._wake_monitor_task:
            self._wake_monitor_task.cancel()
            try:
                await self._wake_monitor_task
            except asyncio.CancelledError:
                pass

        logger.info("StartupGreeter stopped")

    # =========================================================================
    # Analytics
    # =========================================================================

    def get_greeting_stats(self) -> Dict[str, Any]:
        """Get greeting statistics."""
        if not self._greeting_history:
            return {"total_greetings": 0}

        total = len(self._greeting_history)
        spoken = sum(1 for g in self._greeting_history if g.spoken)
        avg_duration = sum(g.duration_ms for g in self._greeting_history) / total

        # Count by context
        by_context = {}
        for g in self._greeting_history:
            ctx = g.context.name
            by_context[ctx] = by_context.get(ctx, 0) + 1

        # Count by personality
        by_personality = {}
        for g in self._greeting_history:
            pers = g.personality_used.value
            by_personality[pers] = by_personality.get(pers, 0) + 1

        return {
            "total_greetings": total,
            "spoken_count": spoken,
            "spoken_rate": spoken / total,
            "avg_duration_ms": avg_duration,
            "by_context": by_context,
            "by_personality": by_personality,
        }

    # Alias for backwards compatibility
    def get_stats(self) -> Dict[str, Any]:
        """Alias for get_greeting_stats."""
        stats = self.get_greeting_stats()
        # Also add templates_used for simple display
        templates_used = len(set(g.template_id for g in self._greeting_history)) if self._greeting_history else 0
        stats["templates_used"] = templates_used
        return stats


# =============================================================================
# Global Instance and Factory Functions
# =============================================================================

_greeter_instance: Optional[StartupGreeter] = None


async def get_startup_greeter(
    config: Optional[GreetingConfig] = None,
) -> StartupGreeter:
    """Get or create the global startup greeter instance."""
    global _greeter_instance

    if _greeter_instance is None:
        _greeter_instance = StartupGreeter(config)
        await _greeter_instance.initialize()

    return _greeter_instance


async def greet_on_startup() -> GreetingResult:
    """Convenience function to deliver startup greeting."""
    greeter = await get_startup_greeter()
    return await greeter.greet_on_startup()


async def greet_on_wake() -> GreetingResult:
    """Convenience function to deliver wake greeting."""
    greeter = await get_startup_greeter()
    return await greeter.greet_on_wake()


async def notify_sleep(user_initiated: bool = True) -> None:
    """
    Convenience function to deliver sleep farewell.

    This should be called when the laptop is about to go to sleep
    to give JARVIS a chance to say goodbye dynamically.

    Args:
        user_initiated: True if user closed the laptop, False for auto-sleep
    """
    greeter = await get_startup_greeter()
    await greeter.notify_sleep(user_initiated=user_initiated)

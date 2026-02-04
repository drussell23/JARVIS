"""
Intelligent Message Generator for JARVIS Loading Server v212.0
===============================================================

Context-aware message generation for better UX during startup.

Features:
- Pattern recognition from historical startups
- Slow component detection with explanations
- Contextual explanations for delays
- Reassuring messages during long operations
- Stage-specific message templates
- Time-of-day aware greetings
- Progress-based message interpolation

Usage:
    from backend.loading_server.message_generator import IntelligentMessageGenerator

    generator = IntelligentMessageGenerator()
    generator.track_stage_start("backend")
    message = generator.generate_message("backend", progress=50)

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional

logger = logging.getLogger("LoadingServer.Messages")


# Message templates by stage
STAGE_MESSAGES: Dict[str, List[str]] = {
    "initializing": [
        "Initializing JARVIS systems...",
        "Preparing the neural pathways...",
        "Warming up the cognitive engines...",
    ],
    "backend": [
        "Starting JARVIS backend services...",
        "Initializing core systems and APIs...",
        "Loading backend configuration...",
    ],
    "frontend": [
        "Preparing the user interface...",
        "Loading frontend components...",
        "Rendering the JARVIS dashboard...",
    ],
    "models": [
        "Loading AI models... This may take a moment.",
        "Initializing neural networks...",
        "Preparing language models...",
    ],
    "jarvis_prime": [
        "JARVIS Prime brain initializing...",
        "Loading local LLM models...",
        "Preparing the Prime intelligence layer...",
    ],
    "reactor_core": [
        "Reactor-Core orchestrator starting...",
        "Initializing Trinity integration...",
        "Loading code generation models...",
    ],
    "database": [
        "Connecting to database...",
        "Initializing data persistence layer...",
        "Loading cached data...",
    ],
    "websocket": [
        "Establishing real-time connections...",
        "Setting up WebSocket channels...",
        "Preparing live update streams...",
    ],
    "health_check": [
        "Running health diagnostics...",
        "Verifying system integrity...",
        "Checking component status...",
    ],
    "complete": [
        "All systems online!",
        "JARVIS is ready to assist you.",
        "Startup complete - Welcome!",
    ],
}

# Slow stage explanations
SLOW_EXPLANATIONS: Dict[str, str] = {
    "models": "AI models are being loaded into memory. This is normal on first run or cold start.",
    "jarvis_prime": "The Prime brain requires loading large language models. This is resource-intensive.",
    "reactor_core": "Reactor-Core is initializing its code generation pipeline.",
    "backend": "Backend services are starting up and performing initial health checks.",
    "frontend": "The frontend is compiling and optimizing assets.",
    "database": "Database connections are being established and schemas verified.",
}

# Greeting by time of day
TIME_GREETINGS: Dict[str, List[str]] = {
    "morning": [
        "Good morning!",
        "Starting your day with JARVIS...",
        "Rise and shine! JARVIS is booting up...",
    ],
    "afternoon": [
        "Good afternoon!",
        "JARVIS is getting ready...",
        "Afternoon startup initiated...",
    ],
    "evening": [
        "Good evening!",
        "Evening session starting...",
        "JARVIS is preparing for the evening...",
    ],
    "night": [
        "Working late? JARVIS is here to help.",
        "Night owl mode activated...",
        "Burning the midnight oil? Let's get to work.",
    ],
}


@dataclass
class StageHistory:
    """Historical data for a stage."""

    durations: Deque[float] = field(default_factory=lambda: deque(maxlen=20))

    @property
    def average_duration(self) -> Optional[float]:
        """Get average duration."""
        if not self.durations:
            return None
        return sum(self.durations) / len(self.durations)

    @property
    def max_duration(self) -> Optional[float]:
        """Get maximum duration."""
        return max(self.durations) if self.durations else None


@dataclass
class IntelligentMessageGenerator:
    """
    Intelligent context-aware message generation.

    Uses historical data and current system state to generate
    helpful, contextual messages during startup.

    Features:
    - Pattern recognition from historical startups
    - Slow component detection
    - Contextual explanations for delays
    - Reassuring messages during long operations
    """

    message_interval: float = 5.0  # Update message every 5s during long stages

    _historical_durations: Dict[str, StageHistory] = field(init=False, default_factory=dict)
    _current_stage: Optional[str] = field(init=False, default=None)
    _current_stage_start: Optional[float] = field(init=False, default=None)
    _last_message_time: float = field(init=False, default=0.0)
    _last_message: str = field(init=False, default="")
    _startup_time: Optional[datetime] = field(init=False, default=None)
    _message_rotation_index: Dict[str, int] = field(init=False, default_factory=dict)

    def __post_init__(self):
        """Initialize generator."""
        self._startup_time = datetime.now()

    def track_stage_start(self, stage: str) -> None:
        """
        Track when a stage starts.

        Args:
            stage: Stage name
        """
        self._current_stage = stage
        self._current_stage_start = time.time()
        self._last_message_time = time.time()

        if stage not in self._historical_durations:
            self._historical_durations[stage] = StageHistory()

    def track_stage_end(self, stage: str) -> None:
        """
        Track when a stage ends and record duration.

        Args:
            stage: Stage name
        """
        if self._current_stage_start and stage == self._current_stage:
            duration = time.time() - self._current_stage_start
            self._historical_durations[stage].durations.append(duration)

        self._current_stage = None
        self._current_stage_start = None

    def generate_message(
        self,
        stage: str,
        component: Optional[str] = None,
        elapsed: Optional[float] = None,
        progress: Optional[float] = None,
        custom_data: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate intelligent contextual message.

        Args:
            stage: Current stage name
            component: Optional component name (jarvis_prime, reactor_core)
            elapsed: Optional elapsed time in current stage
            progress: Optional progress percentage (0-100)
            custom_data: Optional custom data for message generation

        Returns:
            Contextual message string
        """
        now = time.time()

        # Use elapsed time if not provided
        if elapsed is None and self._current_stage_start:
            elapsed = now - self._current_stage_start

        # Get historical average for this stage
        avg_duration = None
        if stage in self._historical_durations:
            avg_duration = self._historical_durations[stage].average_duration

        # If elapsed time available, compare to average
        if elapsed and avg_duration:
            if elapsed > avg_duration * 1.5:
                return self._generate_slow_stage_message(
                    stage, component, elapsed, avg_duration, progress
                )
            elif elapsed > avg_duration * 0.5:
                return self._generate_in_progress_message(stage, component, progress)

        # Check if we should rotate messages for long stages
        if self._should_rotate_message(now):
            return self._generate_rotating_message(stage, component, progress)

        # Default message
        return self._generate_default_message(stage, component, progress)

    def _should_rotate_message(self, now: float) -> bool:
        """Check if we should rotate to a new message."""
        return now - self._last_message_time >= self.message_interval

    def _generate_slow_stage_message(
        self,
        stage: str,
        component: Optional[str],
        elapsed: float,
        avg_duration: float,
        progress: Optional[float],
    ) -> str:
        """Generate message for stages taking longer than expected."""
        name = self._get_display_name(stage, component)

        # Get explanation if available
        explanation_key = component or stage
        explanation = SLOW_EXPLANATIONS.get(explanation_key, "")

        # Provide context based on how much slower
        if elapsed > avg_duration * 3:
            base = f"{name} is taking longer than usual (typically {avg_duration:.1f}s)."
            if explanation:
                return f"{base} {explanation}"
            return f"{base} This may be due to cold start or resource constraints..."
        elif elapsed > avg_duration * 2:
            return f"{name} startup in progress... Usually takes {avg_duration:.1f}s, currently at {elapsed:.1f}s..."
        else:
            return f"{name} loading... Almost there (average: {avg_duration:.1f}s)..."

    def _generate_in_progress_message(
        self,
        stage: str,
        component: Optional[str],
        progress: Optional[float],
    ) -> str:
        """Generate message for stages in progress."""
        # Component-specific messages
        if component == "jarvis_prime":
            return "JARVIS Prime brain initializing... Loading local LLM models..."
        elif component == "reactor_core":
            return "Reactor-Core orchestrator starting... Initializing Trinity integration..."

        # Stage-specific messages
        messages = STAGE_MESSAGES.get(stage, [])
        if messages:
            return self._get_rotating_message(stage, messages)

        # Default
        name = self._get_display_name(stage, component)
        if progress:
            return f"{name} in progress... ({progress:.0f}%)"
        return f"{name} in progress..."

    def _generate_rotating_message(
        self,
        stage: str,
        component: Optional[str],
        progress: Optional[float],
    ) -> str:
        """Generate a rotating message for long stages."""
        self._last_message_time = time.time()

        messages = STAGE_MESSAGES.get(stage, [])
        if messages:
            return self._get_rotating_message(stage, messages)

        return self._generate_default_message(stage, component, progress)

    def _generate_default_message(
        self,
        stage: str,
        component: Optional[str],
        progress: Optional[float],
    ) -> str:
        """Generate default message for a stage."""
        # Try stage-specific messages first
        messages = STAGE_MESSAGES.get(stage, [])
        if messages:
            return self._get_rotating_message(stage, messages)

        # Generate from name
        name = self._get_display_name(stage, component)

        if progress and progress >= 100:
            return f"{name} complete!"
        elif progress:
            return f"{name}... ({progress:.0f}%)"
        else:
            return f"{name}..."

    def _get_rotating_message(self, key: str, messages: List[str]) -> str:
        """Get a rotating message from a list."""
        if key not in self._message_rotation_index:
            self._message_rotation_index[key] = 0

        index = self._message_rotation_index[key]
        message = messages[index % len(messages)]
        self._message_rotation_index[key] = index + 1

        return message

    def _get_display_name(self, stage: str, component: Optional[str]) -> str:
        """Get display name for stage/component."""
        if component:
            return component.replace("_", " ").title()
        return stage.replace("_", " ").title()

    def get_greeting(self) -> str:
        """
        Get time-of-day appropriate greeting.

        Returns:
            Greeting message
        """
        hour = datetime.now().hour

        if 5 <= hour < 12:
            time_period = "morning"
        elif 12 <= hour < 17:
            time_period = "afternoon"
        elif 17 <= hour < 21:
            time_period = "evening"
        else:
            time_period = "night"

        greetings = TIME_GREETINGS.get(time_period, TIME_GREETINGS["morning"])
        return random.choice(greetings)

    def get_completion_message(self, duration: Optional[float] = None) -> str:
        """
        Get completion message.

        Args:
            duration: Total startup duration in seconds

        Returns:
            Completion message
        """
        messages = STAGE_MESSAGES.get("complete", ["Ready!"])
        base = random.choice(messages)

        if duration:
            return f"{base} (Started in {duration:.1f}s)"
        return base

    def get_error_message(self, error: str, stage: Optional[str] = None) -> str:
        """
        Generate user-friendly error message.

        Args:
            error: Error description
            stage: Stage where error occurred

        Returns:
            User-friendly error message
        """
        stage_name = self._get_display_name(stage, None) if stage else "Startup"

        friendly_messages = {
            "timeout": f"{stage_name} timed out. The system may be under heavy load.",
            "connection": f"Could not connect during {stage_name}. Checking network...",
            "permission": f"Permission issue during {stage_name}. Check file permissions.",
            "memory": f"Memory issue during {stage_name}. Try closing other applications.",
            "port": f"Port conflict during {stage_name}. Another service may be running.",
        }

        # Check for known error patterns
        error_lower = error.lower()
        for key, msg in friendly_messages.items():
            if key in error_lower:
                return msg

        return f"Error during {stage_name}: {error}"

    def get_recovery_message(self, stage: str, attempt: int) -> str:
        """
        Generate recovery/retry message.

        Args:
            stage: Stage being retried
            attempt: Retry attempt number

        Returns:
            Recovery message
        """
        name = self._get_display_name(stage, None)

        if attempt <= 2:
            return f"Retrying {name}... (attempt {attempt})"
        elif attempt <= 4:
            return f"Still working on {name}... (attempt {attempt})"
        else:
            return f"{name} is taking longer than expected. Attempt {attempt}..."

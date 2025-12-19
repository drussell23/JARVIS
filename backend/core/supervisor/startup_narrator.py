#!/usr/bin/env python3
"""
JARVIS Intelligent Startup Narrator v1.0
=========================================

Provides intelligent, phase-aware voice narration during JARVIS startup.
Coordinates with the visual loading page to provide complementary
(not redundant) audio feedback.

Features:
- Phase-aware narration with smart batching
- Adaptive timing based on startup speed
- Progress milestone announcements
- Error and recovery narration
- Dynamic message generation (no hardcoding)
- Console and voice output coordination
- User activity awareness
- Parallel execution support

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from collections import deque

logger = logging.getLogger(__name__)


class StartupPhase(str, Enum):
    """Startup phases with semantic meaning."""
    SUPERVISOR_INIT = "supervisor_init"
    CLEANUP = "cleanup"
    SPAWNING = "spawning"
    BACKEND_INIT = "backend_init"
    DATABASE = "database"
    DOCKER = "docker"
    MODELS = "models"
    VOICE = "voice"
    VISION = "vision"
    FRONTEND = "frontend"
    WEBSOCKET = "websocket"
    COMPLETE = "complete"
    FAILED = "failed"
    RECOVERY = "recovery"


class NarrationPriority(Enum):
    """Priority levels for narration messages."""
    LOW = auto()       # Background info, can be skipped
    MEDIUM = auto()    # Standard updates
    HIGH = auto()      # Important milestones
    CRITICAL = auto()  # Must announce (errors, completion)


@dataclass
class NarrationConfig:
    """Configuration for startup narration - all dynamic, no hardcoding."""
    
    # Enable/disable channels
    voice_enabled: bool = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE", "true").lower() == "true"
    )
    console_enabled: bool = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_CONSOLE", "true").lower() == "true"
    )
    
    # Timing controls (seconds)
    min_narration_interval: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_NARRATOR_MIN_INTERVAL", "3.0"))
    )
    max_narration_interval: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_NARRATOR_MAX_INTERVAL", "30.0"))
    )
    
    # Progress thresholds for milestone announcements
    progress_milestones: List[int] = field(
        default_factory=lambda: [25, 50, 75, 100]
    )
    
    # TTS settings
    voice: str = field(
        default_factory=lambda: os.getenv("STARTUP_NARRATOR_VOICE_NAME", "Daniel")
    )
    rate: int = field(
        default_factory=lambda: int(os.getenv("STARTUP_NARRATOR_RATE", "190"))
    )
    
    # Behavior settings
    narrate_slow_phases: bool = True  # Announce when phase takes long
    slow_phase_threshold: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_SLOW_PHASE_THRESHOLD", "15.0"))
    )
    
    # Skip phases that complete too quickly
    skip_fast_phases: bool = True
    fast_phase_threshold: float = field(
        default_factory=lambda: float(os.getenv("STARTUP_FAST_PHASE_THRESHOLD", "1.0"))
    )


@dataclass
class PhaseInfo:
    """Information about a startup phase."""
    phase: StartupPhase
    message: str
    progress: float
    start_time: datetime = field(default_factory=datetime.now)
    end_time: Optional[datetime] = None
    narrated: bool = False
    duration_seconds: float = 0.0
    
    @property
    def is_complete(self) -> bool:
        return self.end_time is not None
    
    def complete(self):
        self.end_time = datetime.now()
        self.duration_seconds = (self.end_time - self.start_time).total_seconds()


# Dynamic narration templates - organized by phase and context
PHASE_NARRATION_TEMPLATES: Dict[StartupPhase, Dict[str, List[str]]] = {
    StartupPhase.SUPERVISOR_INIT: {
        "start": [
            "Lifecycle supervisor online. Initializing core systems.",
            "Supervisor active. Preparing JARVIS environment.",
            "System supervisor initialized. Beginning startup sequence.",
        ],
    },
    StartupPhase.CLEANUP: {
        "start": [
            "Cleaning up previous sessions.",
            "Preparing a fresh workspace.",
        ],
        "complete": [
            "Cleanup complete.",
        ],
    },
    StartupPhase.SPAWNING: {
        "start": [
            "Spawning JARVIS core process.",
            "Initializing main system.",
            "Launching JARVIS backend.",
        ],
    },
    StartupPhase.BACKEND_INIT: {
        "start": [
            "Initializing backend services.",
            "Backend is coming online.",
        ],
        "complete": [
            "Backend services initialized.",
        ],
    },
    StartupPhase.DATABASE: {
        "start": [
            "Connecting to databases.",
        ],
        "complete": [
            "Database connections established.",
        ],
    },
    StartupPhase.DOCKER: {
        "start": [
            "Initializing Docker environment.",
            "Starting container services.",
        ],
        "slow": [
            "Docker is taking a moment. Please stand by.",
            "Waiting for Docker daemon. This may take a minute.",
        ],
        "complete": [
            "Docker environment ready.",
            "Container services online.",
        ],
    },
    StartupPhase.MODELS: {
        "start": [
            "Loading machine learning models.",
            "Initializing neural networks.",
        ],
        "slow": [
            "Loading models. This is the heavy lifting.",
            "Neural networks are warming up.",
        ],
        "complete": [
            "Models loaded and ready.",
            "Neural networks initialized.",
        ],
    },
    StartupPhase.VOICE: {
        "start": [
            "Initializing voice systems.",
        ],
        "complete": [
            "Voice recognition ready.",
            "I can hear you now.",
        ],
    },
    StartupPhase.VISION: {
        "start": [
            "Calibrating vision systems.",
        ],
        "complete": [
            "Vision systems online.",
        ],
    },
    StartupPhase.FRONTEND: {
        "start": [
            "Connecting to user interface.",
        ],
        "complete": [
            "Interface connected.",
        ],
    },
    StartupPhase.WEBSOCKET: {
        "start": [
            "Establishing real-time connections.",
        ],
        "complete": [
            "Real-time connections active.",
        ],
    },
    StartupPhase.COMPLETE: {
        "complete": [
            "JARVIS online. All systems operational.",
            "Good to be back, Sir. How may I assist you?",
            "Systems restored. Ready when you are.",
            "Initialization complete. At your service.",
        ],
    },
    StartupPhase.FAILED: {
        "error": [
            "I've encountered a problem during startup.",
            "Something went wrong. Attempting recovery.",
            "Startup failed. Let me try again.",
        ],
    },
    StartupPhase.RECOVERY: {
        "start": [
            "Initiating recovery sequence.",
            "Attempting to recover from failure.",
        ],
        "complete": [
            "Recovery successful.",
        ],
    },
}

# Progress milestone templates
MILESTONE_TEMPLATES: Dict[int, List[str]] = {
    25: [
        "About a quarter of the way through.",
        "25 percent loaded.",
    ],
    50: [
        "Halfway there.",
        "50 percent complete.",
    ],
    75: [
        "Almost ready. Just a few more moments.",
        "75 percent. Nearly done.",
    ],
    100: [
        # Use COMPLETE phase templates instead
    ],
}

# Slow startup encouragement
SLOW_STARTUP_MESSAGES: List[str] = [
    "Taking a bit longer than usual. Everything is fine.",
    "Still working on it. Thank you for your patience.",
    "Loading additional components. Almost there.",
]


class IntelligentStartupNarrator:
    """
    Intelligent narrator that provides phase-aware voice feedback during startup.
    
    Features:
    - Smart batching to avoid over-narration
    - Adaptive timing based on phase duration
    - Milestone announcements
    - Error and recovery handling
    - Parallel execution support
    
    Example:
        >>> narrator = IntelligentStartupNarrator()
        >>> await narrator.start()
        >>> await narrator.announce_phase(StartupPhase.DOCKER, "Starting Docker", 20)
        >>> await narrator.announce_progress(50, "Loading models")
        >>> await narrator.announce_complete()
    """
    
    def __init__(self, config: Optional[NarrationConfig] = None):
        self.config = config or NarrationConfig()
        self._is_macos = platform.system() == "Darwin"
        
        # State tracking
        self._phases: Dict[StartupPhase, PhaseInfo] = {}
        self._current_phase: Optional[StartupPhase] = None
        self._last_narration_time: Optional[datetime] = None
        self._last_progress_narrated: int = 0
        self._startup_start_time: Optional[datetime] = None
        self._narration_history: deque = deque(maxlen=50)
        
        # Async management
        self._narration_queue: asyncio.Queue = asyncio.Queue()
        self._processor_task: Optional[asyncio.Task] = None
        self._current_speech_process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        
        # Tracking for intelligent decisions
        self._phases_narrated: Set[StartupPhase] = set()
        self._milestones_announced: Set[int] = set()
        self._slow_phase_announced: bool = False
        
        logger.info(f"🎙️ Startup narrator initialized (voice: {self.config.voice}, enabled: {self.config.voice_enabled})")
    
    async def start(self) -> None:
        """Start the narration processor."""
        self._startup_start_time = datetime.now()
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_narration_queue())
        logger.debug("🎙️ Startup narrator started")
    
    async def stop(self) -> None:
        """Stop the narrator and cleanup."""
        if self._current_speech_process:
            try:
                self._current_speech_process.terminate()
                await self._current_speech_process.wait()
            except Exception:
                pass
        
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        
        logger.debug("🎙️ Startup narrator stopped")
    
    async def _process_narration_queue(self) -> None:
        """Process queued narrations in order."""
        while True:
            try:
                text, priority = await self._narration_queue.get()
                await self._speak(text, priority)
                self._narration_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Narration queue error: {e}")
    
    async def _speak(self, text: str, priority: NarrationPriority = NarrationPriority.MEDIUM) -> None:
        """
        Speak text using macOS say command or log on other platforms.
        
        Args:
            text: Text to speak
            priority: Priority level (affects whether we wait or skip)
        """
        async with self._lock:
            # Check minimum interval (unless critical)
            if priority != NarrationPriority.CRITICAL and self._last_narration_time:
                elapsed = (datetime.now() - self._last_narration_time).total_seconds()
                if elapsed < self.config.min_narration_interval:
                    logger.debug(f"🎙️ Skipping narration (too soon): {text[:50]}...")
                    return
            
            # Console output
            if self.config.console_enabled:
                logger.info(f"🔊 Narrating: {text}")
            
            # Track history
            self._narration_history.append({
                "text": text,
                "priority": priority.name,
                "timestamp": datetime.now().isoformat(),
            })
            
            # Voice output
            if self.config.voice_enabled and self._is_macos:
                try:
                    cmd = [
                        "say",
                        "-v", self.config.voice,
                        "-r", str(self.config.rate),
                        text,
                    ]
                    
                    self._current_speech_process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    
                    # Wait for critical messages, fire-and-forget for others
                    if priority == NarrationPriority.CRITICAL:
                        await self._current_speech_process.wait()
                    else:
                        # Fire and forget, but track the process
                        asyncio.create_task(self._current_speech_process.wait())
                    
                except Exception as e:
                    logger.warning(f"TTS error: {e}")
                finally:
                    self._last_narration_time = datetime.now()
            else:
                self._last_narration_time = datetime.now()
    
    async def _queue_narration(
        self,
        text: str,
        priority: NarrationPriority = NarrationPriority.MEDIUM,
    ) -> None:
        """Queue a narration for processing."""
        await self._narration_queue.put((text, priority))
    
    def _get_phase_message(
        self,
        phase: StartupPhase,
        context: str = "start",
    ) -> Optional[str]:
        """Get a random message for a phase and context."""
        phase_templates = PHASE_NARRATION_TEMPLATES.get(phase, {})
        templates = phase_templates.get(context, [])
        
        if templates:
            return random.choice(templates)
        return None
    
    def _should_narrate_phase(self, phase: StartupPhase) -> bool:
        """Determine if we should narrate this phase."""
        # Always narrate first phase
        if not self._phases_narrated:
            return True
        
        # Always narrate completion and errors
        if phase in (StartupPhase.COMPLETE, StartupPhase.FAILED, StartupPhase.RECOVERY):
            return True
        
        # Check if already narrated
        if phase in self._phases_narrated:
            return False
        
        return True
    
    async def announce_phase(
        self,
        phase: StartupPhase,
        message: str,
        progress: float,
        context: str = "start",
        priority: NarrationPriority = NarrationPriority.MEDIUM,
    ) -> None:
        """
        Announce a startup phase transition.
        
        Args:
            phase: The startup phase
            message: Progress message (for logging)
            progress: Current progress percentage
            context: Narration context (start, complete, slow, error)
            priority: Narration priority
        """
        # Track phase info
        if phase not in self._phases:
            self._phases[phase] = PhaseInfo(
                phase=phase,
                message=message,
                progress=progress,
            )
        
        # Complete previous phase
        if self._current_phase and self._current_phase != phase:
            prev_info = self._phases.get(self._current_phase)
            if prev_info and not prev_info.is_complete:
                prev_info.complete()
                
                # Optionally announce completion of slow phases
                if (
                    self.config.narrate_slow_phases
                    and prev_info.duration_seconds > self.config.slow_phase_threshold
                    and context != "complete"
                ):
                    complete_msg = self._get_phase_message(self._current_phase, "complete")
                    if complete_msg:
                        await self._queue_narration(complete_msg, NarrationPriority.LOW)
        
        self._current_phase = phase
        
        # Decide whether to narrate
        if not self._should_narrate_phase(phase):
            logger.debug(f"🎙️ Skipping phase narration (already narrated): {phase.value}")
            return
        
        # Get narration text
        narration_text = self._get_phase_message(phase, context)
        
        if narration_text:
            self._phases_narrated.add(phase)
            self._phases[phase].narrated = True
            await self._queue_narration(narration_text, priority)
    
    async def announce_progress(
        self,
        progress: float,
        message: Optional[str] = None,
    ) -> None:
        """
        Announce progress milestone if reached.
        
        Args:
            progress: Current progress percentage (0-100)
            message: Optional message to include
        """
        progress_int = int(progress)
        
        # Check for milestone
        for milestone in self.config.progress_milestones:
            if (
                milestone <= progress_int
                and milestone > self._last_progress_narrated
                and milestone not in self._milestones_announced
                and milestone < 100  # 100% uses complete handler
            ):
                self._milestones_announced.add(milestone)
                self._last_progress_narrated = milestone
                
                # Get milestone message
                templates = MILESTONE_TEMPLATES.get(milestone, [])
                if templates:
                    text = random.choice(templates)
                    await self._queue_narration(text, NarrationPriority.LOW)
                break
    
    async def announce_slow_startup(self) -> None:
        """Announce that startup is taking longer than expected."""
        if not self._slow_phase_announced:
            self._slow_phase_announced = True
            text = random.choice(SLOW_STARTUP_MESSAGES)
            await self._queue_narration(text, NarrationPriority.LOW)
    
    async def announce_complete(
        self,
        message: Optional[str] = None,
        duration_seconds: Optional[float] = None,
    ) -> None:
        """
        Announce startup completion.
        
        Args:
            message: Optional custom completion message
            duration_seconds: Total startup duration
        """
        # Complete any remaining phase
        if self._current_phase:
            info = self._phases.get(self._current_phase)
            if info and not info.is_complete:
                info.complete()
        
        self._current_phase = StartupPhase.COMPLETE
        
        # Use custom message or pick from templates
        if message:
            text = message
        else:
            text = self._get_phase_message(StartupPhase.COMPLETE, "complete") or "JARVIS online."
        
        # Add duration context for long startups
        if duration_seconds and duration_seconds > 30:
            duration_min = int(duration_seconds // 60)
            if duration_min > 0:
                text = f"{text} Startup took {duration_min} minute{'s' if duration_min > 1 else ''}."
        
        await self._speak(text, NarrationPriority.CRITICAL)
    
    async def announce_error(
        self,
        error_message: str,
        phase: Optional[StartupPhase] = None,
    ) -> None:
        """
        Announce a startup error.
        
        Args:
            error_message: Error description
            phase: Phase where error occurred
        """
        text = self._get_phase_message(StartupPhase.FAILED, "error") or "Startup failed."
        await self._speak(text, NarrationPriority.CRITICAL)
    
    async def announce_recovery(self, success: bool = True) -> None:
        """Announce recovery attempt result."""
        if success:
            text = self._get_phase_message(StartupPhase.RECOVERY, "complete") or "Recovery successful."
        else:
            text = "Recovery failed. Please check the logs."
        await self._speak(text, NarrationPriority.HIGH)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get narrator statistics."""
        return {
            "phases_narrated": list(self._phases_narrated),
            "milestones_announced": list(self._milestones_announced),
            "current_phase": self._current_phase.value if self._current_phase else None,
            "narration_count": len(self._narration_history),
            "startup_duration": (
                (datetime.now() - self._startup_start_time).total_seconds()
                if self._startup_start_time else None
            ),
            "history": list(self._narration_history)[-10:],  # Last 10 entries
        }


# Phase mapping from progress reporter stages to StartupPhase
STAGE_TO_PHASE: Dict[str, StartupPhase] = {
    "init": StartupPhase.BACKEND_INIT,
    "supervisor_init": StartupPhase.SUPERVISOR_INIT,
    "cleanup": StartupPhase.CLEANUP,
    "spawning": StartupPhase.SPAWNING,
    "backend": StartupPhase.BACKEND_INIT,
    "api": StartupPhase.BACKEND_INIT,
    "database": StartupPhase.DATABASE,
    "docker": StartupPhase.DOCKER,
    "models": StartupPhase.MODELS,
    "voice": StartupPhase.VOICE,
    "vision": StartupPhase.VISION,
    "frontend": StartupPhase.FRONTEND,
    "websocket": StartupPhase.WEBSOCKET,
    "complete": StartupPhase.COMPLETE,
    "failed": StartupPhase.FAILED,
    "error": StartupPhase.FAILED,
}


def get_phase_from_stage(stage: str) -> StartupPhase:
    """Convert a progress reporter stage to a StartupPhase."""
    return STAGE_TO_PHASE.get(stage.lower(), StartupPhase.BACKEND_INIT)


# Singleton instance
_startup_narrator: Optional[IntelligentStartupNarrator] = None


def get_startup_narrator(config: Optional[NarrationConfig] = None) -> IntelligentStartupNarrator:
    """Get the singleton startup narrator instance."""
    global _startup_narrator
    if _startup_narrator is None:
        _startup_narrator = IntelligentStartupNarrator(config)
    return _startup_narrator


async def narrate_phase(
    phase: StartupPhase,
    message: str,
    progress: float,
    context: str = "start",
) -> None:
    """Convenience function to narrate a phase."""
    narrator = get_startup_narrator()
    await narrator.announce_phase(phase, message, progress, context)


async def narrate_progress(progress: float, message: Optional[str] = None) -> None:
    """Convenience function to narrate progress."""
    narrator = get_startup_narrator()
    await narrator.announce_progress(progress, message)


async def narrate_complete(message: Optional[str] = None) -> None:
    """Convenience function to narrate completion."""
    narrator = get_startup_narrator()
    await narrator.announce_complete(message)


async def narrate_error(error_message: str) -> None:
    """Convenience function to narrate an error."""
    narrator = get_startup_narrator()
    await narrator.announce_error(error_message)


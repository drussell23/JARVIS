"""
Ghost Hands Narration Engine
==============================

The "voice" of JARVIS Ghost Hands - provides real-time humanistic narration
following the "Working Out Loud" philosophy.

Narration Philosophy:
- Perception: "I see the build failed on Space 3."
- Intent: "I'm going to check the logs."
- Action: "Restarting the server now."
- Confirmation: "Server is back up. You're good to go."

Features:
- Real-time event-driven narration
- Integration with N-Optic Nerve (vision events)
- Integration with Background Actuator (action events)
- Contextual phrase generation
- Personality/tone configuration
- Verbosity levels (silent, minimal, normal, verbose)
- Priority-based narration (critical > important > informational)
- Debouncing to prevent over-narrating
- Environment-driven configuration

Architecture:
    NarrationEngine (Singleton)
    ├── PhraseGenerator (contextual phrases)
    │   ├── Perception phrases
    │   ├── Intent phrases
    │   ├── Action phrases
    │   └── Confirmation phrases
    ├── TTSBridge (voice output)
    │   └── Trinity Voice Coordinator -> macOS say -> UnifiedTTSEngine
    ├── NarrationQueue (priority queue)
    │   └── Debouncing & deduplication
    └── EventSubscriber (event listeners)

Author: JARVIS AI System
Version: 1.0.0 - Ghost Hands Edition
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import shutil
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class VerbosityLevel(Enum):
    """Narration verbosity levels."""
    SILENT = 0      # No narration
    MINIMAL = 1     # Only critical events
    NORMAL = 2      # Important events (default)
    VERBOSE = 3     # All events including informational
    DEBUG = 4       # Everything, for debugging


class NarrationPriority(Enum):
    """Priority levels for narration items."""
    CRITICAL = 1    # Must be spoken immediately (errors, security)
    HIGH = 2        # Important events (task completion, warnings)
    NORMAL = 3      # Standard events (status updates)
    LOW = 4         # Informational (minor events)
    BACKGROUND = 5  # Can be skipped if busy


class NarrationTone(Enum):
    """Personality tones for narration."""
    PROFESSIONAL = "professional"
    FRIENDLY = "friendly"
    CONCISE = "concise"
    DETAILED = "detailed"


@dataclass
class NarrationConfig:
    """Configuration for the Narration Engine."""

    # Verbosity
    verbosity: VerbosityLevel = field(
        default_factory=lambda: VerbosityLevel[
            os.getenv("JARVIS_NARRATION_VERBOSITY", "NORMAL").upper()
        ]
    )

    # Tone
    tone: NarrationTone = field(
        default_factory=lambda: NarrationTone(
            os.getenv("JARVIS_NARRATION_TONE", "friendly")
        )
    )

    # TTS settings
    tts_enabled: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_NARRATION_TTS", "true"
        ).lower() == "true"
    )
    tts_use_trinity: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_NARRATION_USE_TRINITY", "true"
        ).lower() == "true"
    )
    tts_voice: Optional[str] = field(
        default_factory=lambda: (
            os.getenv("JARVIS_NARRATION_VOICE")
            or os.getenv("JARVIS_NARRATOR_VOICE_NAME")
            or os.getenv("JARVIS_VOICE_NAME")
            or "Daniel"
        )
    )
    tts_speed: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_NARRATION_SPEED", "1.1"))
    )

    # Debouncing
    debounce_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_NARRATION_DEBOUNCE_MS", "1500"))
    )
    max_queue_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_NARRATION_QUEUE_SIZE", "20"))
    )

    # Event filtering
    skip_repetitive: bool = field(
        default_factory=lambda: os.getenv(
            "JARVIS_NARRATION_SKIP_REPETITIVE", "true"
        ).lower() == "true"
    )
    repetitive_window_ms: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_NARRATION_REPETITIVE_MS", "5000"))
    )

    # User name for personalization
    user_name: str = field(
        default_factory=lambda: os.getenv("JARVIS_USER_NAME", "")
    )


# =============================================================================
# Narration Items
# =============================================================================

class NarrationType(Enum):
    """Types of narration."""
    PERCEPTION = auto()     # What JARVIS sees
    INTENT = auto()         # What JARVIS plans to do
    ACTION = auto()         # What JARVIS is doing
    CONFIRMATION = auto()   # Result of an action
    STATUS = auto()         # System status
    WARNING = auto()        # Warnings
    ERROR = auto()          # Errors
    GREETING = auto()       # Greetings and farewells


@dataclass
class NarrationItem:
    """A single narration item to be spoken."""
    text: str
    narration_type: NarrationType
    priority: NarrationPriority
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    spoken: bool = False
    skip_if_busy: bool = False

    @property
    def age_ms(self) -> float:
        """Get age of this item in milliseconds."""
        return (datetime.now() - self.timestamp).total_seconds() * 1000

    def __lt__(self, other: "NarrationItem") -> bool:
        """Compare by priority for queue ordering."""
        return self.priority.value < other.priority.value


# =============================================================================
# Phrase Generator
# =============================================================================

class PhraseGenerator:
    """
    Generates contextual phrases for narration.

    Produces natural-sounding phrases based on event type, context,
    and configured tone/personality.
    """

    def __init__(self, config: NarrationConfig):
        self.config = config
        self._phrase_templates = self._build_phrase_templates()

    def _build_phrase_templates(self) -> Dict[str, Dict[str, List[str]]]:
        """Build phrase templates for different tones and types."""
        return {
            # PERCEPTION phrases
            "perception": {
                "friendly": [
                    "I see {description}.",
                    "Looks like {description}.",
                    "I noticed {description}.",
                    "Just spotted {description}.",
                ],
                "professional": [
                    "Detected: {description}.",
                    "Observation: {description}.",
                    "Visual confirmation: {description}.",
                ],
                "concise": [
                    "{description} detected.",
                    "Seeing {description}.",
                ],
                "detailed": [
                    "I'm observing {description} in {location}.",
                    "My visual scan shows {description}.",
                ],
            },

            # INTENT phrases
            "intent": {
                "friendly": [
                    "I'm going to {action}.",
                    "Let me {action}.",
                    "I'll {action} for you.",
                    "Going to {action} now.",
                ],
                "professional": [
                    "Initiating: {action}.",
                    "Proceeding to {action}.",
                    "Action planned: {action}.",
                ],
                "concise": [
                    "Will {action}.",
                    "{action}...",
                ],
                "detailed": [
                    "Based on what I see, I'm going to {action}.",
                    "My plan is to {action} to address this.",
                ],
            },

            # ACTION phrases
            "action": {
                "friendly": [
                    "{action} now.",
                    "Working on {action}.",
                    "Doing {action}.",
                    "On it! {action}.",
                ],
                "professional": [
                    "Executing: {action}.",
                    "In progress: {action}.",
                    "Performing {action}.",
                ],
                "concise": [
                    "{action}...",
                    "{action}.",
                ],
                "detailed": [
                    "Currently {action}. This should take {estimate}.",
                    "I'm now {action}. Please wait...",
                ],
            },

            # CONFIRMATION phrases
            "confirmation": {
                "friendly": [
                    "Done! {result}.",
                    "All set. {result}.",
                    "Finished. {result}.",
                    "Got it! {result}.",
                ],
                "professional": [
                    "Completed: {result}.",
                    "Task finished: {result}.",
                    "Operation successful: {result}.",
                ],
                "concise": [
                    "{result}.",
                    "Done.",
                ],
                "detailed": [
                    "I've completed the task. {result}",
                    "Everything is done. {result}. Let me know if you need anything else.",
                ],
            },

            # ERROR phrases
            "error": {
                "friendly": [
                    "Oops! {error}.",
                    "Something went wrong. {error}.",
                    "Hmm, {error}. Let me try again.",
                    "I hit a snag. {error}.",
                ],
                "professional": [
                    "Error encountered: {error}.",
                    "Failure: {error}.",
                    "Exception: {error}.",
                ],
                "concise": [
                    "Error: {error}.",
                    "{error}.",
                ],
                "detailed": [
                    "I encountered an error: {error}. I'll attempt recovery.",
                    "Something went wrong: {error}. Analyzing the issue now.",
                ],
            },

            # SUCCESS phrases
            "success": {
                "friendly": [
                    "Great news! {message}.",
                    "Success! {message}.",
                    "Perfect. {message}.",
                    "Excellent! {message}.",
                ],
                "professional": [
                    "Success: {message}.",
                    "Operation complete: {message}.",
                    "Confirmed: {message}.",
                ],
                "concise": [
                    "{message}.",
                    "Done.",
                ],
                "detailed": [
                    "The operation completed successfully. {message}.",
                    "Everything worked as expected. {message}.",
                ],
            },

            # WARNING phrases
            "warning": {
                "friendly": [
                    "Heads up! {warning}.",
                    "Just so you know, {warning}.",
                    "Quick warning: {warning}.",
                ],
                "professional": [
                    "Warning: {warning}.",
                    "Attention: {warning}.",
                    "Notice: {warning}.",
                ],
                "concise": [
                    "{warning}.",
                ],
                "detailed": [
                    "I need to warn you about something: {warning}.",
                    "There's an issue you should know about: {warning}.",
                ],
            },

            # STATUS phrases
            "status": {
                "friendly": [
                    "{status}.",
                    "Just checking in. {status}.",
                    "Quick update: {status}.",
                ],
                "professional": [
                    "Status: {status}.",
                    "Update: {status}.",
                ],
                "concise": [
                    "{status}.",
                ],
                "detailed": [
                    "Here's the current status: {status}.",
                ],
            },

            # GREETING phrases
            "greeting": {
                "friendly": [
                    "Hey{name}! Ghost Hands is ready.",
                    "Good to see you{name}! I'm watching the screens.",
                    "Hi{name}! I'm here and monitoring.",
                ],
                "professional": [
                    "Ghost Hands activated{name}.",
                    "System ready{name}. Monitoring initiated.",
                ],
                "concise": [
                    "Ready{name}.",
                    "Online.",
                ],
                "detailed": [
                    "Hello{name}! Ghost Hands is now active. I'm monitoring your windows and ready to help.",
                ],
            },
        }

    def generate(
        self,
        phrase_type: str,
        context: Dict[str, Any],
    ) -> str:
        """
        Generate a phrase based on type and context.

        Args:
            phrase_type: Type of phrase (perception, intent, action, etc.)
            context: Dictionary with context values to fill in

        Returns:
            Generated phrase string
        """
        tone = self.config.tone.value
        templates = self._phrase_templates.get(phrase_type, {}).get(tone, [])

        if not templates:
            # Fallback to friendly tone
            templates = self._phrase_templates.get(phrase_type, {}).get("friendly", [])

        if not templates:
            # Last resort - return context description
            return context.get("description", context.get("message", "Working..."))

        # Select a template
        template = random.choice(templates)

        # Add user name if configured
        name_suffix = f", {self.config.user_name}" if self.config.user_name else ""
        context["name"] = name_suffix

        # Fill in template
        try:
            return template.format(**context)
        except KeyError as e:
            logger.warning(f"Missing context key for phrase: {e}")
            # Return template with unfilled placeholders
            return re.sub(r"\{[^}]+\}", "", template)

    def perception(self, description: str, location: str = "") -> str:
        """Generate a perception phrase."""
        return self.generate("perception", {
            "description": description,
            "location": location or "the screen",
        })

    def intent(self, action: str) -> str:
        """Generate an intent phrase."""
        return self.generate("intent", {"action": action})

    def action(self, action: str, estimate: str = "a moment") -> str:
        """Generate an action phrase."""
        return self.generate("action", {
            "action": action,
            "estimate": estimate,
        })

    def confirmation(self, result: str) -> str:
        """Generate a confirmation phrase."""
        return self.generate("confirmation", {"result": result})

    def error(self, error: str) -> str:
        """Generate an error phrase."""
        return self.generate("error", {"error": error})

    def success(self, message: str) -> str:
        """Generate a success phrase."""
        return self.generate("success", {"message": message})

    def warning(self, warning: str) -> str:
        """Generate a warning phrase."""
        return self.generate("warning", {"warning": warning})

    def status(self, status: str) -> str:
        """Generate a status phrase."""
        return self.generate("status", {"status": status})

    def greeting(self) -> str:
        """Generate a greeting phrase."""
        return self.generate("greeting", {})


# =============================================================================
# TTS Bridge
# =============================================================================

class TTSBridge:
    """
    Bridge to the TTS engine for voice output.

    Provides async speaking with queue management and interruption support.
    """

    def __init__(self, config: NarrationConfig):
        self.config = config
        self._tts_engine = None
        self._voice_coordinator = None
        self._voice_context = None
        self._voice_priority = {}
        self._use_trinity = False
        self._use_macos_say = False
        self._say_voice = self._resolve_voice_name()
        self._say_rate_wpm = self._resolve_rate_wpm()
        self._initialized = False
        self._speaking = False
        self._speak_lock = asyncio.Lock()

    async def _get_tts(self):
        """Get the TTS singleton (lazy init)."""
        if self._tts_engine is None:
            try:
                from voice.engines.unified_tts_engine import get_tts_engine
            except ImportError:
                try:
                    from backend.voice.engines.unified_tts_engine import get_tts_engine
                except ImportError:
                    return None
            try:
                self._tts_engine = await get_tts_engine()
            except Exception as e:
                logger.debug(f"TTS singleton unavailable: {e}")
        return self._tts_engine

    def _resolve_voice_name(self) -> str:
        """Resolve canonical narration voice with JARVIS fallback chain."""
        return (
            self.config.tts_voice
            or os.getenv("JARVIS_NARRATOR_VOICE_NAME")
            or os.getenv("JARVIS_VOICE_NAME")
            or "Daniel"
        )

    def _resolve_rate_wpm(self) -> int:
        """Resolve narration speech rate (words per minute)."""
        narrator_rate = os.getenv("JARVIS_NARRATOR_VOICE_RATE")
        if narrator_rate:
            try:
                return max(120, min(260, int(narrator_rate)))
            except ValueError:
                pass

        legacy_rate = os.getenv("JARVIS_VOICE_RATE_WPM")
        if legacy_rate:
            try:
                return max(120, min(260, int(legacy_rate)))
            except ValueError:
                pass

        derived_rate = int(round(175 * max(0.5, min(2.0, self.config.tts_speed))))
        return max(120, min(260, derived_rate))

    async def initialize(self) -> bool:
        """Initialize the narration voice backend with deterministic fallback."""
        if self._initialized:
            return True

        if not self.config.tts_enabled:
            logger.info("[NARRATION] TTS disabled by configuration")
            return False

        self._say_voice = self._resolve_voice_name()
        self._say_rate_wpm = self._resolve_rate_wpm()

        # Ensure narrator profile defaults are present before coordinator bootstrap.
        os.environ.setdefault("JARVIS_NARRATOR_VOICE_NAME", self._say_voice)
        os.environ.setdefault("JARVIS_NARRATOR_VOICE_RATE", str(self._say_rate_wpm))

        # 1) Preferred path: Trinity Voice Coordinator (shared control plane).
        if self.config.tts_use_trinity:
            try:
                try:
                    from backend.core.trinity_voice_coordinator import (
                        VoiceContext,
                        VoicePriority,
                        get_voice_coordinator,
                    )
                except ImportError:
                    from core.trinity_voice_coordinator import (  # type: ignore
                        VoiceContext,
                        VoicePriority,
                        get_voice_coordinator,
                    )

                self._voice_coordinator = await get_voice_coordinator()
                self._voice_context = VoiceContext.NARRATOR
                self._voice_priority = {
                    True: VoicePriority.NORMAL,
                    False: VoicePriority.HIGH,
                }
                self._use_trinity = True
                self._initialized = True
                logger.info(
                    "[NARRATION] TTS bridge initialized via Trinity Voice Coordinator "
                    f"(voice={self._say_voice}, rate={self._say_rate_wpm} WPM)"
                )
                return True
            except Exception as e:
                logger.warning(f"[NARRATION] Trinity voice path unavailable: {e}")

        # 2) Fallback path on macOS: direct 'say' command (avoids playback static).
        if shutil.which("say"):
            self._use_macos_say = True
            self._initialized = True
            logger.info(
                "[NARRATION] TTS bridge initialized via macOS say "
                f"(voice={self._say_voice}, rate={self._say_rate_wpm} WPM)"
            )
            return True

        # 3) Last resort: legacy unified TTS engine (lazy singleton).
        try:
            tts = await self._get_tts()
            if tts:
                # Configure voice if specified
                if self.config.tts_voice:
                    tts.set_voice(self.config.tts_voice)

                if self.config.tts_speed != 1.0:
                    tts.set_speed(self.config.tts_speed)

                self._initialized = True
                logger.info("[NARRATION] TTS bridge initialized via UnifiedTTSEngine")
                return True

            logger.warning("[NARRATION] UnifiedTTSEngine not available")
            return False
        except Exception as e:
            logger.error(f"[NARRATION] TTS initialization failed: {e}")
            return False

    async def speak(self, text: str, interruptible: bool = True) -> bool:
        """
        Speak the given text.

        Args:
            text: Text to speak
            interruptible: Whether this speech can be interrupted

        Returns:
            True if speech completed, False if interrupted or failed
        """
        if not self._initialized:
            logger.debug(f"[NARRATION] (No TTS) Would say: {text}")
            return False

        async with self._speak_lock:
            self._speaking = True
            try:
                if self._use_trinity and self._voice_coordinator and self._voice_context:
                    priority = self._voice_priority.get(interruptible) or self._voice_priority.get(True)
                    if priority is None:
                        logger.warning("[NARRATION] Trinity priority profile missing, skipping narration")
                        return False
                    success, reason = await self._voice_coordinator.announce(
                        message=text,
                        context=self._voice_context,
                        priority=priority,
                        source="ghost_hands",
                        metadata={"interruptible": interruptible, "component": "narration_engine"},
                    )
                    if not success:
                        logger.warning(f"[NARRATION] Trinity voice rejected narration: {reason}")
                    return success

                if self._use_macos_say:
                    cmd = [
                        "say",
                        "-v", self._say_voice,
                        "-r", str(self._say_rate_wpm),
                        text,
                    ]
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        return True

                    err = stderr.decode(errors="ignore").strip() if stderr else ""
                    logger.error(f"[NARRATION] macOS say failed ({proc.returncode}): {err}")
                    return False

                tts = await self._get_tts()
                if tts:
                    await tts.speak(text, play_audio=True)
                    return True

                logger.debug(f"[NARRATION] (No active backend) Would say: {text}")
                return False
            except Exception as e:
                logger.error(f"[NARRATION] TTS speak failed: {e}")
                return False
            finally:
                self._speaking = False

    @property
    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        return self._speaking

    async def cleanup(self) -> None:
        """Clean up TTS resources."""
        if self._tts_engine:
            await self._tts_engine.cleanup()
        self._tts_engine = None
        self._voice_coordinator = None
        self._use_trinity = False
        self._use_macos_say = False
        self._initialized = False


# =============================================================================
# Narration Engine (Main Class)
# =============================================================================

class NarrationEngine:
    """
    Ghost Hands Narration Engine: Real-time humanistic voice feedback.

    Provides contextual narration for vision events and actions,
    following the "Working Out Loud" philosophy.
    """

    _instance: Optional["NarrationEngine"] = None

    def __new__(cls, config: Optional[NarrationConfig] = None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, config: Optional[NarrationConfig] = None):
        if self._initialized:
            return

        self.config = config or NarrationConfig()

        # Components
        self._phrase_generator = PhraseGenerator(self.config)
        self._tts_bridge = TTSBridge(self.config)

        # Narration queue (priority queue)
        self._narration_queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=self.config.max_queue_size
        )
        self._processor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Deduplication
        self._recent_narrations: Deque[Tuple[str, datetime]] = deque(maxlen=50)

        # State
        self._is_running = False
        self._paused = False

        # Statistics
        self._stats = {
            "total_narrations": 0,
            "spoken_narrations": 0,
            "skipped_narrations": 0,
            "errors": 0,
            "start_time": None,
        }

        self._initialized = True
        logger.info("[NARRATION] Narration Engine initialized")

    @classmethod
    def get_instance(cls, config: Optional[NarrationConfig] = None) -> "NarrationEngine":
        """Get singleton instance."""
        return cls(config)

    async def start(self) -> bool:
        """Start the narration engine."""
        if self._is_running:
            return True

        logger.info("[NARRATION] Starting Narration Engine...")

        # Initialize TTS
        await self._tts_bridge.initialize()

        # Start processor
        self._shutdown_event.clear()
        self._processor_task = asyncio.create_task(
            self._process_queue(),
            name="NarrationProcessor"
        )

        self._is_running = True
        self._stats["start_time"] = datetime.now()

        # Speak greeting
        if self.config.verbosity.value >= VerbosityLevel.NORMAL.value:
            await self.narrate_greeting()

        logger.info("[NARRATION] Narration Engine started")
        return True

    async def stop(self) -> None:
        """Stop the narration engine."""
        logger.info("[NARRATION] Stopping Narration Engine...")

        self._shutdown_event.set()

        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
            try:
                await asyncio.wait_for(self._processor_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

        await self._tts_bridge.cleanup()
        self._is_running = False

        logger.info("[NARRATION] Narration Engine stopped")

    def pause(self) -> None:
        """Pause narration (events still queue but don't speak)."""
        self._paused = True
        logger.info("[NARRATION] Paused")

    def resume(self) -> None:
        """Resume narration."""
        self._paused = False
        logger.info("[NARRATION] Resumed")

    # =========================================================================
    # High-Level Narration Methods
    # =========================================================================

    async def narrate_greeting(self) -> None:
        """Narrate a greeting."""
        text = self._phrase_generator.greeting()
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.GREETING,
            priority=NarrationPriority.NORMAL,
        )

    async def narrate_perception(
        self,
        description: str,
        location: str = "",
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate a perception (what JARVIS sees)."""
        if self.config.verbosity.value < VerbosityLevel.NORMAL.value:
            return

        text = self._phrase_generator.perception(description, location)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.PERCEPTION,
            priority=priority,
            context={"description": description, "location": location},
        )

    async def narrate_intent(
        self,
        action: str,
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate an intent (what JARVIS plans to do)."""
        if self.config.verbosity.value < VerbosityLevel.NORMAL.value:
            return

        text = self._phrase_generator.intent(action)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.INTENT,
            priority=priority,
            context={"action": action},
        )

    async def narrate_action(
        self,
        action: str,
        estimate: str = "a moment",
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate an action in progress."""
        if self.config.verbosity.value < VerbosityLevel.VERBOSE.value:
            # Only speak actions in verbose mode
            return

        text = self._phrase_generator.action(action, estimate)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.ACTION,
            priority=priority,
            context={"action": action},
        )

    async def narrate_confirmation(
        self,
        result: str,
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate a confirmation (result of an action)."""
        if self.config.verbosity.value < VerbosityLevel.MINIMAL.value:
            return

        text = self._phrase_generator.confirmation(result)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.CONFIRMATION,
            priority=priority,
            context={"result": result},
        )

    async def narrate_error(
        self,
        error: str,
        priority: NarrationPriority = NarrationPriority.HIGH,
    ) -> None:
        """Narrate an error."""
        if self.config.verbosity.value < VerbosityLevel.MINIMAL.value:
            return

        text = self._phrase_generator.error(error)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.ERROR,
            priority=priority,
            context={"error": error},
        )

    async def narrate_success(
        self,
        message: str,
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate a success."""
        if self.config.verbosity.value < VerbosityLevel.NORMAL.value:
            return

        text = self._phrase_generator.success(message)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.CONFIRMATION,
            priority=priority,
            context={"message": message},
        )

    async def narrate_warning(
        self,
        warning: str,
        priority: NarrationPriority = NarrationPriority.HIGH,
    ) -> None:
        """Narrate a warning."""
        if self.config.verbosity.value < VerbosityLevel.MINIMAL.value:
            return

        text = self._phrase_generator.warning(warning)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.WARNING,
            priority=priority,
            context={"warning": warning},
        )

    async def narrate_status(
        self,
        status: str,
        priority: NarrationPriority = NarrationPriority.LOW,
    ) -> None:
        """Narrate a status update."""
        if self.config.verbosity.value < VerbosityLevel.VERBOSE.value:
            return

        text = self._phrase_generator.status(status)
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.STATUS,
            priority=priority,
            context={"status": status},
            skip_if_busy=True,
        )

    async def narrate_custom(
        self,
        text: str,
        priority: NarrationPriority = NarrationPriority.NORMAL,
    ) -> None:
        """Narrate custom text."""
        await self._queue_narration(
            text=text,
            narration_type=NarrationType.STATUS,
            priority=priority,
        )

    # =========================================================================
    # Event Handlers (for integration with N-Optic Nerve and Actuator)
    # =========================================================================

    async def on_vision_event(self, event: Any) -> None:
        """
        Handle a vision event from N-Optic Nerve.

        Args:
            event: VisionEvent from N-Optic Nerve
        """
        try:
            # Import here to avoid circular imports
            from ghost_hands.n_optic_nerve import VisionEventType

            event_type = event.event_type
            app_name = event.app_name
            matched = event.matched_pattern or event.detected_text[:50] if event.detected_text else ""

            # Generate description based on event type
            if event_type == VisionEventType.SUCCESS_DETECTED:
                await self.narrate_perception(
                    f"success message in {app_name}",
                    f"Space {event.space_id}",
                    priority=NarrationPriority.NORMAL,
                )
            elif event_type == VisionEventType.ERROR_DETECTED:
                await self.narrate_perception(
                    f"an error in {app_name}: {matched}",
                    f"Space {event.space_id}",
                    priority=NarrationPriority.HIGH,
                )
            elif event_type == VisionEventType.TEXT_DETECTED:
                await self.narrate_perception(
                    f"'{matched}' appeared in {app_name}",
                    f"Space {event.space_id}",
                    priority=NarrationPriority.LOW,
                )
            elif event_type == VisionEventType.LOADING_COMPLETE:
                await self.narrate_perception(
                    f"{app_name} finished loading",
                    priority=NarrationPriority.NORMAL,
                )

        except Exception as e:
            logger.error(f"[NARRATION] Error handling vision event: {e}")

    async def on_action_report(self, report: Any) -> None:
        """
        Handle an action report from Background Actuator.

        Args:
            report: ActionReport from Background Actuator
        """
        try:
            from ghost_hands.background_actuator import ActionResult

            action_str = str(report.action)
            result = report.result

            if result == ActionResult.SUCCESS:
                await self.narrate_confirmation(
                    f"{action_str} completed",
                    priority=NarrationPriority.NORMAL,
                )
            elif result == ActionResult.FAILED:
                await self.narrate_error(
                    f"{action_str} failed: {report.error or 'unknown error'}",
                    priority=NarrationPriority.HIGH,
                )
            elif result == ActionResult.TIMEOUT:
                await self.narrate_warning(
                    f"{action_str} timed out",
                    priority=NarrationPriority.HIGH,
                )
            elif result == ActionResult.FOCUS_STOLEN:
                await self.narrate_warning(
                    "Focus was stolen during action, restoring",
                    priority=NarrationPriority.NORMAL,
                )

        except Exception as e:
            logger.error(f"[NARRATION] Error handling action report: {e}")

    # =========================================================================
    # Internal Methods
    # =========================================================================

    async def _queue_narration(
        self,
        text: str,
        narration_type: NarrationType,
        priority: NarrationPriority,
        context: Dict[str, Any] = None,
        skip_if_busy: bool = False,
    ) -> bool:
        """Queue a narration item."""
        self._stats["total_narrations"] += 1

        # Check for duplicates
        if self.config.skip_repetitive and self._is_duplicate(text):
            self._stats["skipped_narrations"] += 1
            logger.debug(f"[NARRATION] Skipped duplicate: {text[:30]}...")
            return False

        item = NarrationItem(
            text=text,
            narration_type=narration_type,
            priority=priority,
            context=context or {},
            skip_if_busy=skip_if_busy,
        )

        try:
            # Use priority value as queue priority (lower = higher priority)
            self._narration_queue.put_nowait((priority.value, item))
            self._record_narration(text)
            return True
        except asyncio.QueueFull:
            self._stats["skipped_narrations"] += 1
            logger.warning("[NARRATION] Queue full, dropping narration")
            return False

    def _is_duplicate(self, text: str) -> bool:
        """Check if text is a duplicate of recent narrations."""
        cutoff = datetime.now() - timedelta(
            milliseconds=self.config.repetitive_window_ms
        )

        for recent_text, timestamp in self._recent_narrations:
            if timestamp > cutoff and recent_text == text:
                return True

        return False

    def _record_narration(self, text: str) -> None:
        """Record a narration for deduplication."""
        self._recent_narrations.append((text, datetime.now()))

    async def _process_queue(self) -> None:
        """Process narration queue."""
        while not self._shutdown_event.is_set():
            try:
                # Get next item (blocks until available)
                try:
                    priority_value, item = await asyncio.wait_for(
                        self._narration_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Skip if paused
                if self._paused:
                    continue

                # Skip old items
                if item.age_ms > 5000:
                    self._stats["skipped_narrations"] += 1
                    continue

                # Skip low-priority items if busy
                if item.skip_if_busy and self._tts_bridge.is_speaking:
                    self._stats["skipped_narrations"] += 1
                    continue

                # Debounce
                await asyncio.sleep(self.config.debounce_ms / 1000.0)

                # Speak the narration
                logger.info(f"[NARRATION] Speaking: {item.text}")
                success = await self._tts_bridge.speak(item.text)

                if success:
                    self._stats["spoken_narrations"] += 1
                    item.spoken = True

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[NARRATION] Queue processing error: {e}")
                self._stats["errors"] += 1

    # =========================================================================
    # Configuration and Stats
    # =========================================================================

    def set_verbosity(self, level: VerbosityLevel) -> None:
        """Set verbosity level."""
        self.config.verbosity = level
        logger.info(f"[NARRATION] Verbosity set to {level.name}")

    def set_tone(self, tone: NarrationTone) -> None:
        """Set narration tone."""
        self.config.tone = tone
        self._phrase_generator = PhraseGenerator(self.config)
        logger.info(f"[NARRATION] Tone set to {tone.value}")

    def get_stats(self) -> Dict[str, Any]:
        """Get narration statistics."""
        return {
            **self._stats,
            "is_running": self._is_running,
            "is_paused": self._paused,
            "queue_size": self._narration_queue.qsize(),
            "verbosity": self.config.verbosity.name,
            "tone": self.config.tone.value,
        }


# =============================================================================
# Convenience Functions
# =============================================================================

async def get_narration_engine(
    config: Optional[NarrationConfig] = None
) -> NarrationEngine:
    """Get the Narration Engine singleton instance."""
    engine = NarrationEngine.get_instance(config)
    if not engine._is_running:
        await engine.start()
    return engine


# =============================================================================
# Testing
# =============================================================================

async def test_narration_engine():
    """Test the Narration Engine."""
    print("=" * 60)
    print("Testing Narration Engine")
    print("=" * 60)

    # Configure for testing
    config = NarrationConfig(
        verbosity=VerbosityLevel.VERBOSE,
        tone=NarrationTone.FRIENDLY,
        debounce_ms=500,
    )

    engine = await get_narration_engine(config)

    # Test different narration types
    print("\n1. Testing perception narration...")
    await engine.narrate_perception(
        "the build failed",
        "Terminal on Space 2",
        priority=NarrationPriority.HIGH,
    )
    await asyncio.sleep(2)

    print("\n2. Testing intent narration...")
    await engine.narrate_intent("check the error logs")
    await asyncio.sleep(2)

    print("\n3. Testing action narration...")
    await engine.narrate_action("analyzing the logs")
    await asyncio.sleep(2)

    print("\n4. Testing confirmation narration...")
    await engine.narrate_confirmation("Found the issue - missing dependency")
    await asyncio.sleep(2)

    print("\n5. Testing error narration...")
    await engine.narrate_error("npm install failed")
    await asyncio.sleep(2)

    print("\n6. Testing success narration...")
    await engine.narrate_success("Build completed successfully")
    await asyncio.sleep(2)

    # Show stats
    print("\n7. Statistics:")
    stats = engine.get_stats()
    for key, value in stats.items():
        print(f"   {key}: {value}")

    await engine.stop()
    print("\nTest complete!")


if __name__ == "__main__":
    asyncio.run(test_narration_engine())

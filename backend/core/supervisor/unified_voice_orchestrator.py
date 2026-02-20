#!/usr/bin/env python3
"""
JARVIS Unified Voice Orchestrator v2.0 - Intelligent Speech Edition
====================================================================

Production-grade voice coordination system that ensures ONLY ONE VOICE
speaks at a time across the entire JARVIS system.

ROOT CAUSE ADDRESSED:
    Multiple narrator systems (SupervisorNarrator, IntelligentStartupNarrator,
    speak_tts, etc.) were spawning concurrent `say` processes, causing
    overlapping voices ("hallucinating multiple voices").

SOLUTION:
    Single source of truth for ALL voice output with:
    - Global speech lock (only one `say` process at a time)
    - Priority-based queue (critical messages take precedence)
    - Intelligent deduplication (avoid redundant announcements)
    - Async-safe singleton pattern
    - Process lifecycle management (proper cleanup)

v2.0 ENHANCEMENTS:
    - Speech State Machine: Tracks conversation context to prevent topic repetition
    - Semantic Deduplication: Groups similar messages (not just exact matches)
    - Message Coalescing: Combines rapid-fire messages into summaries
    - Natural Pacing: Adds intelligent pauses between messages
    - Topic Cooldown: Prevents repeating the same topic too frequently
    - Intelligent Summarization: Batches multiple similar messages

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                  UnifiedVoiceOrchestrator                     │
    │  (Singleton - Single Source of Truth for ALL Voice Output)    │
    ├──────────────────────────────────────────────────────────────┤
    │                                                              │
    │   ┌─────────────┐    ┌─────────────┐    ┌─────────────┐     │
    │   │ Supervisor  │    │  Startup    │    │    Other    │     │
    │   │  Narrator   │───▶│  Narrator   │───▶│  Speakers   │     │
    │   └─────────────┘    └─────────────┘    └─────────────┘     │
    │          │                  │                  │             │
    │          └──────────────────┼──────────────────┘             │
    │                             ▼                                │
    │                  ┌─────────────────────┐                    │
    │                  │   Priority Queue    │                    │
    │                  │  (CRITICAL > HIGH   │                    │
    │                  │   > MEDIUM > LOW)   │                    │
    │                  └──────────┬──────────┘                    │
    │                             │                                │
    │                             ▼                                │
    │                  ┌─────────────────────┐                    │
    │                  │   Global Speech     │                    │
    │                  │      Lock           │                    │
    │                  │  (asyncio.Lock)     │                    │
    │                  └──────────┬──────────┘                    │
    │                             │                                │
    │                             ▼                                │
    │                  ┌─────────────────────┐                    │
    │                  │   Single `say`      │                    │
    │                  │   Process           │                    │
    │                  │   (macOS TTS)       │                    │
    │                  └─────────────────────┘                    │
    │                                                              │
    └──────────────────────────────────────────────────────────────┘

Features:
- Single speech queue across ALL components
- Priority-based scheduling (CRITICAL interrupts, others queue)
- Intelligent deduplication with time window
- Rate limiting to prevent rapid-fire announcements
- Process lifecycle management
- Graceful degradation on non-macOS
- Comprehensive logging and metrics
- Dynamic configuration via environment variables

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import platform
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from backend.core.async_safety import LazyAsyncLock
from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "false") -> bool:
    """Parse common truthy env values."""
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _canonical_voice_name() -> str:
    """Canonical voice identity for supervisor-controlled speech."""
    if _env_flag("JARVIS_FORCE_DANIEL_VOICE", "true"):
        return "Daniel"
    value = os.getenv("JARVIS_CANONICAL_VOICE_NAME", "Daniel").strip()
    return value or "Daniel"


def _resolve_voice_name(requested: Optional[str] = None) -> str:
    """
    Resolve effective voice for orchestrator.

    By default we enforce canonical voice to avoid drift across subsystems.
    """
    canonical = _canonical_voice_name()
    if _env_flag("JARVIS_ENFORCE_CANONICAL_VOICE", "true"):
        return canonical

    if requested and requested.strip():
        return requested.strip()

    env_voice = os.getenv("JARVIS_VOICE_NAME", "").strip()
    return env_voice or canonical


class VoicePriority(Enum):
    """Priority levels for voice messages."""
    LOW = 1        # Background info, can be skipped/delayed
    MEDIUM = 2     # Standard updates
    HIGH = 3       # Important milestones
    CRITICAL = 4   # Must announce immediately (errors, completion)

    def __lt__(self, other):
        if isinstance(other, VoicePriority):
            return self.value < other.value
        return NotImplemented

    def __gt__(self, other):
        if isinstance(other, VoicePriority):
            return self.value > other.value
        return NotImplemented


class VoiceSource(str, Enum):
    """Source of voice message for tracking and deduplication."""
    SUPERVISOR = "supervisor"
    STARTUP = "startup"
    UPDATE = "update"
    HEALTH = "health"
    MAINTENANCE = "maintenance"
    AUTHENTICATION = "authentication"
    INTENT = "intent"
    SYSTEM = "system"
    EXTERNAL = "external"
    # v2.0: Zero-Touch Autonomous Update System
    AUTONOMOUS = "autonomous"          # Zero-Touch autonomous updates
    DEAD_MAN_SWITCH = "dead_man_switch" # Post-update stability monitoring
    PRIME_DIRECTIVES = "prime_directives"  # Safety constraint violations
    # v3.0: Data Flywheel and Learning System
    FLYWHEEL = "flywheel"              # Self-improving data flywheel
    TRAINING = "training"              # Model training/fine-tuning
    LEARNING = "learning"              # Learning goals and discovery
    JARVIS_PRIME = "jarvis_prime"      # JARVIS-Prime tier-0 brain
    # v79.0: Coding Council Evolution System
    CODING_COUNCIL = "coding_council"  # Coding Council self-evolution
    EVOLUTION = "evolution"            # Code evolution operations


class SpeechTopic(str, Enum):
    """Topics for semantic grouping and cooldown tracking."""
    STARTUP = "startup"
    SHUTDOWN = "shutdown"
    UPDATE = "update"
    ROLLBACK = "rollback"
    ERROR = "error"
    HEALTH = "health"
    AUTHENTICATION = "authentication"
    ZERO_TOUCH = "zero_touch"
    DMS = "dms"
    PROGRESS = "progress"
    GENERAL = "general"
    # v3.0: Data Flywheel and Learning System
    FLYWHEEL = "flywheel"              # Self-improving data collection
    TRAINING = "training"              # Model training announcements
    LEARNING = "learning"              # Learning goals and discovery
    SCRAPING = "scraping"              # Web scraping progress
    MODEL_DEPLOY = "model_deploy"      # Model deployment announcements
    INTELLIGENCE = "intelligence"      # JARVIS-Prime intelligent responses
    # v79.0: Coding Council Evolution System
    EVOLUTION = "evolution"            # Code evolution progress/completion
    CODING_COUNCIL = "coding_council"  # Coding Council operations


@dataclass
class VoiceConfig:
    """Configuration for the unified voice orchestrator."""

    # Enable/disable voice
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_ENABLED", "true").lower() == "true"
    )

    # TTS settings
    voice: str = field(
        default_factory=lambda: _resolve_voice_name()
    )
    rate: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_VOICE_RATE", "180"))
    )

    # Rate limiting
    min_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_MIN_INTERVAL", "2.0"))
    )

    # Deduplication
    dedup_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_DEDUP_WINDOW", "30.0"))
    )
    dedup_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_DEDUP", "true").lower() == "true"
    )

    # Queue limits
    max_queue_size: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_VOICE_QUEUE_SIZE", "50"))
    )
    queue_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_QUEUE_TIMEOUT", "60.0"))
    )

    # Priority settings
    interrupt_on_critical: bool = True  # Critical messages interrupt current speech
    skip_low_when_busy: bool = True     # Skip LOW priority when queue is large
    
    # v2.0: Intelligent Speech Settings
    topic_cooldown_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_TOPIC_COOLDOWN", "15.0"))
    )
    natural_pause_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_NATURAL_PAUSE", "0.5"))
    )
    coalesce_window_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_COALESCE_WINDOW", "3.0"))
    )
    max_coalesced_messages: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_VOICE_MAX_COALESCE", "5"))
    )
    semantic_dedup_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_SEMANTIC_DEDUP", "true").lower() == "true"
    )
    semantic_similarity_threshold: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_SIMILARITY_THRESHOLD", "0.7"))
    )


@dataclass
class VoiceMessage:
    """A voice message in the queue."""
    text: str
    priority: VoicePriority
    source: VoiceSource
    created_at: datetime = field(default_factory=datetime.now)
    wait_for_completion: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    completion_event: Optional[asyncio.Event] = None
    topic: SpeechTopic = SpeechTopic.GENERAL  # v2.0: Topic for semantic grouping

    # For deduplication
    @property
    def fingerprint(self) -> str:
        """Generate fingerprint for deduplication."""
        # Normalize text (lowercase, strip whitespace)
        normalized = self.text.lower().strip()
        return hashlib.md5(normalized.encode()).hexdigest()[:16]
    
    @property
    def keywords(self) -> Set[str]:
        """Extract keywords for semantic similarity."""
        # Common words to exclude
        stopwords = {
            'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
            'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
            'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'to', 'of',
            'in', 'for', 'on', 'with', 'at', 'by', 'from', 'up', 'about', 'into',
            'through', 'during', 'before', 'after', 'above', 'below', 'between',
            'under', 'again', 'further', 'then', 'once', 'here', 'there', 'when',
            'where', 'why', 'how', 'all', 'each', 'few', 'more', 'most', 'other',
            'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so', 'than',
            'too', 'very', 'just', 'and', 'but', 'if', 'or', 'as', 'i', 'you', 'he',
            'she', 'it', 'we', 'they', 'sir', 'now', 'please', 'moment',
        }
        words = set(self.text.lower().split())
        # Remove stopwords and short words
        return {w for w in words if w not in stopwords and len(w) > 2}

    def __lt__(self, other):
        """For priority queue ordering (higher priority first)."""
        if isinstance(other, VoiceMessage):
            # Higher priority comes first (reverse order)
            if self.priority != other.priority:
                return self.priority.value > other.priority.value
            # Same priority: earlier message first
            return self.created_at < other.created_at
        return NotImplemented


@dataclass
class SpeechContext:
    """
    v2.0: Tracks conversation context for intelligent speech management.
    
    Prevents topic repetition, enables message coalescing, and maintains
    natural conversation flow.
    """
    # Topic cooldown tracking
    topic_last_spoken: Dict[SpeechTopic, float] = field(default_factory=dict)
    
    # Recent message tracking for coalescing
    recent_messages: deque = field(default_factory=lambda: deque(maxlen=20))
    
    # Current speech state
    current_topic: Optional[SpeechTopic] = None
    last_message_time: float = 0.0
    messages_in_burst: int = 0
    
    # Semantic tracking
    recent_keywords: deque = field(default_factory=lambda: deque(maxlen=100))
    
    def is_topic_on_cooldown(self, topic: SpeechTopic, cooldown_seconds: float) -> bool:
        """Check if a topic is on cooldown."""
        if topic not in self.topic_last_spoken:
            return False
        elapsed = time.time() - self.topic_last_spoken[topic]
        return elapsed < cooldown_seconds
    
    def record_topic(self, topic: SpeechTopic) -> None:
        """Record that a topic was just spoken about."""
        self.topic_last_spoken[topic] = time.time()
        self.current_topic = topic
    
    def get_semantic_similarity(self, message: VoiceMessage) -> float:
        """
        Calculate semantic similarity to recent messages.
        Returns 0.0-1.0 where higher = more similar.
        """
        if not self.recent_keywords:
            return 0.0
        
        msg_keywords = message.keywords
        if not msg_keywords:
            return 0.0
        
        # Count overlapping keywords
        recent_set = set(self.recent_keywords)
        overlap = len(msg_keywords & recent_set)
        
        # Normalize by message keyword count
        return overlap / len(msg_keywords) if msg_keywords else 0.0
    
    def record_message(self, message: VoiceMessage) -> None:
        """Record a spoken message."""
        self.recent_messages.append({
            'text': message.text,
            'topic': message.topic,
            'time': time.time(),
        })
        self.recent_keywords.extend(message.keywords)
        self.last_message_time = time.time()
        self.messages_in_burst += 1
    
    def reset_burst(self) -> None:
        """Reset burst counter (after natural pause)."""
        self.messages_in_burst = 0


@dataclass
class VoiceMetrics:
    """Metrics for voice orchestrator."""
    messages_queued: int = 0
    messages_spoken: int = 0
    messages_skipped: int = 0
    messages_deduplicated: int = 0
    total_speak_time_seconds: float = 0.0
    last_message_at: Optional[datetime] = None
    current_queue_size: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "messages_queued": self.messages_queued,
            "messages_spoken": self.messages_spoken,
            "messages_skipped": self.messages_skipped,
            "messages_deduplicated": self.messages_deduplicated,
            "total_speak_time_seconds": round(self.total_speak_time_seconds, 2),
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "current_queue_size": self.current_queue_size,
        }


class UnifiedVoiceOrchestrator:
    """
    Singleton voice orchestrator that coordinates ALL voice output.

    This ensures only one voice speaks at a time across the entire system,
    preventing the "multiple voices" issue caused by concurrent `say` processes.

    Example:
        >>> orchestrator = get_voice_orchestrator()
        >>> await orchestrator.start()
        >>> await orchestrator.speak("Hello world", VoicePriority.MEDIUM, VoiceSource.SYSTEM)
        >>> await orchestrator.speak_and_wait("Critical message", VoicePriority.CRITICAL)
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        """Initialize the unified voice orchestrator."""
        self.config = config or VoiceConfig()
        self.config.voice = _resolve_voice_name(self.config.voice)

        # Keep env aliases in sync so downstream engines (Trinity/TTS) agree.
        if _env_flag("JARVIS_ENFORCE_CANONICAL_VOICE", "true"):
            os.environ["JARVIS_VOICE_NAME"] = self.config.voice
            os.environ["JARVIS_VOICE_FALLBACK_ORDER"] = self.config.voice

        self._is_macos = platform.system() == "Darwin"

        # Core state
        self._initialized = False
        self._running = False
        self._shutting_down = False

        # Global speech lock - THE KEY to preventing concurrent voices
        self._speech_lock = asyncio.Lock()

        # Priority queue for messages
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=self.config.max_queue_size
        )

        # Processing task
        self._processor_task: Optional[asyncio.Task] = None

        # Current speech process
        self._current_process: Optional[asyncio.subprocess.Process] = None

        # Rate limiting
        self._last_speak_time: float = 0.0

        # Deduplication
        self._recent_fingerprints: deque = deque(maxlen=100)
        self._fingerprint_times: Dict[str, float] = {}

        # Metrics
        self._metrics = VoiceMetrics()

        # Callbacks
        self._on_speak_start: List[Callable[[VoiceMessage], None]] = []
        self._on_speak_end: List[Callable[[VoiceMessage, float], None]] = []
        
        # v2.0: Speech context for intelligent management
        self._context = SpeechContext()
        
        # v2.0: Message coalescing buffer
        self._coalesce_buffer: List[VoiceMessage] = []
        self._coalesce_timer: Optional[asyncio.Task] = None
        
        # v2.0: Topic inference patterns
        self._topic_patterns: Dict[str, SpeechTopic] = {
            'startup': SpeechTopic.STARTUP,
            'initializ': SpeechTopic.STARTUP,
            'boot': SpeechTopic.STARTUP,
            'online': SpeechTopic.STARTUP,
            'shutdown': SpeechTopic.SHUTDOWN,
            'stopping': SpeechTopic.SHUTDOWN,
            'goodbye': SpeechTopic.SHUTDOWN,
            'update': SpeechTopic.UPDATE,
            'download': SpeechTopic.UPDATE,
            'install': SpeechTopic.UPDATE,
            'rollback': SpeechTopic.ROLLBACK,
            'revert': SpeechTopic.ROLLBACK,
            'error': SpeechTopic.ERROR,
            'fail': SpeechTopic.ERROR,
            'crash': SpeechTopic.ERROR,
            'health': SpeechTopic.HEALTH,
            'monitor': SpeechTopic.HEALTH,
            'status': SpeechTopic.HEALTH,
            'authent': SpeechTopic.AUTHENTICATION,
            'verif': SpeechTopic.AUTHENTICATION,
            'unlock': SpeechTopic.AUTHENTICATION,
            'zero-touch': SpeechTopic.ZERO_TOUCH,
            'autonomous': SpeechTopic.ZERO_TOUCH,
            'dead man': SpeechTopic.DMS,
            'probation': SpeechTopic.DMS,
            'dms': SpeechTopic.DMS,
            'progress': SpeechTopic.PROGRESS,
            'percent': SpeechTopic.PROGRESS,
            '%': SpeechTopic.PROGRESS,
            # v3.0: Data Flywheel and Learning patterns
            'flywheel': SpeechTopic.FLYWHEEL,
            'self-improv': SpeechTopic.FLYWHEEL,
            'data collect': SpeechTopic.FLYWHEEL,
            'experience': SpeechTopic.FLYWHEEL,
            'training': SpeechTopic.TRAINING,
            'fine-tun': SpeechTopic.TRAINING,
            'model learn': SpeechTopic.TRAINING,
            'neural': SpeechTopic.TRAINING,
            'learning goal': SpeechTopic.LEARNING,
            'discover': SpeechTopic.LEARNING,
            'study': SpeechTopic.LEARNING,
            'knowledge': SpeechTopic.LEARNING,
            'scraping': SpeechTopic.SCRAPING,
            'web data': SpeechTopic.SCRAPING,
            'scout': SpeechTopic.SCRAPING,
            'crawl': SpeechTopic.SCRAPING,
            'deploy model': SpeechTopic.MODEL_DEPLOY,
            'gguf': SpeechTopic.MODEL_DEPLOY,
            'quantiz': SpeechTopic.MODEL_DEPLOY,
            'jarvis-prime': SpeechTopic.INTELLIGENCE,
            'tier-0': SpeechTopic.INTELLIGENCE,
            'intelligent': SpeechTopic.INTELLIGENCE,
        }

        logger.info(
            f"🔊 Unified Voice Orchestrator v2.0 initialized "
            f"(voice: {self.config.voice}, enabled: {self.config.enabled}, "
            f"semantic_dedup: {self.config.semantic_dedup_enabled})"
        )

    async def start(self) -> None:
        """Start the voice orchestrator."""
        if self._running:
            return

        self._running = True
        self._shutting_down = False

        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_queue())

        self._initialized = True
        logger.info("🔊 Unified Voice Orchestrator started")

    async def stop(self) -> None:
        """Stop the voice orchestrator gracefully."""
        self._shutting_down = True
        self._running = False

        # Cancel current speech
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
                await asyncio.wait_for(self._current_process.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self._current_process.kill()
                except ProcessLookupError:
                    pass

        # Cancel processor task
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        # Clear queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        logger.info("🔊 Unified Voice Orchestrator stopped")

    async def speak(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.MEDIUM,
        source: VoiceSource = VoiceSource.SYSTEM,
        wait: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
        topic: Optional[SpeechTopic] = None,
    ) -> bool:
        """
        Queue a message to be spoken with intelligent filtering.

        Args:
            text: Text to speak
            priority: Message priority
            source: Source of the message
            wait: If True, wait for message to be spoken
            metadata: Additional context
            topic: Optional speech topic for grouping (auto-inferred if not provided)

        Returns:
            True if message was queued, False if skipped/deduplicated
        """
        if not text or not text.strip():
            return False

        if not self.config.enabled:
            logger.debug(f"🔇 Voice disabled, skipping: {sanitize_for_log(text, 50)}")
            return False
        
        # v2.0: Infer topic if not provided
        inferred_topic = topic or self._infer_topic(text)

        # Create message
        completion_event = asyncio.Event() if wait else None
        message = VoiceMessage(
            text=text.strip(),
            priority=priority,
            source=source,
            wait_for_completion=wait,
            metadata=metadata or {},
            completion_event=completion_event,
            topic=inferred_topic,
        )

        # Check deduplication (exact match)
        if self.config.dedup_enabled and self._is_duplicate(message):
            logger.debug(f"🔇 Deduplicated (exact): {sanitize_for_log(text, 50)}")
            self._metrics.messages_deduplicated += 1
            if completion_event:
                completion_event.set()  # Don't leave caller waiting
            return False
        
        # v2.0: Check semantic similarity (skip if too similar to recent)
        if self.config.semantic_dedup_enabled and priority != VoicePriority.CRITICAL:
            similarity = self._context.get_semantic_similarity(message)
            if similarity > self.config.semantic_similarity_threshold:
                logger.debug(
                    f"🔇 Deduplicated (semantic, {similarity:.2f}): {sanitize_for_log(text, 50)}"
                )
                self._metrics.messages_deduplicated += 1
                if completion_event:
                    completion_event.set()
                return False
        
        # v2.0: Check topic cooldown (skip if same topic spoken recently)
        if priority not in (VoicePriority.CRITICAL, VoicePriority.HIGH):
            if self._context.is_topic_on_cooldown(
                inferred_topic, self.config.topic_cooldown_seconds
            ):
                logger.debug(
                    f"🔇 Topic on cooldown ({inferred_topic.value}): {sanitize_for_log(text, 50)}"
                )
                self._metrics.messages_skipped += 1
                if completion_event:
                    completion_event.set()
                return False

        # Check queue capacity for low priority
        if (
            self.config.skip_low_when_busy
            and priority == VoicePriority.LOW
            and self._queue.qsize() > self.config.max_queue_size // 2
        ):
            logger.debug(f"🔇 Queue busy, skipping low priority: {sanitize_for_log(text, 50)}")
            self._metrics.messages_skipped += 1
            if completion_event:
                completion_event.set()
            return False

        # Queue the message
        try:
            # Use priority tuple for ordering
            await asyncio.wait_for(
                self._queue.put(message),
                timeout=1.0
            )
            self._metrics.messages_queued += 1
            self._metrics.current_queue_size = self._queue.qsize()

            logger.debug(
                f"🔊 Queued ({priority.name}, {source.value}, {inferred_topic.value}): "
                f"{sanitize_for_log(text, 50)} (queue size: {self._queue.qsize()})"
            )

            # Handle critical interrupt
            if priority == VoicePriority.CRITICAL and self.config.interrupt_on_critical:
                await self._interrupt_current()

            # Wait for completion if requested
            if wait and completion_event:
                await completion_event.wait()

            return True

        except asyncio.TimeoutError:
            logger.warning("🔇 Queue full, message dropped")
            self._metrics.messages_skipped += 1
            if completion_event:
                completion_event.set()
            return False
        except asyncio.QueueFull:
            logger.warning("🔇 Queue full, message dropped")
            self._metrics.messages_skipped += 1
            if completion_event:
                completion_event.set()
            return False
    
    def _infer_topic(self, text: str) -> SpeechTopic:
        """Infer the topic of a message from its content."""
        text_lower = text.lower()
        
        for pattern, topic in self._topic_patterns.items():
            if pattern in text_lower:
                return topic
        
        return SpeechTopic.GENERAL

    async def speak_and_wait(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.MEDIUM,
        source: VoiceSource = VoiceSource.SYSTEM,
        metadata: Optional[Dict[str, Any]] = None,
        topic: Optional[SpeechTopic] = None,
    ) -> bool:
        """Speak and wait for completion."""
        return await self.speak(
            text, priority, source, wait=True, metadata=metadata, topic=topic
        )
    
    async def speak_if_not_busy(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.LOW,
        source: VoiceSource = VoiceSource.SYSTEM,
        topic: Optional[SpeechTopic] = None,
    ) -> bool:
        """
        v2.0: Speak only if not currently speaking and queue is empty.
        
        Use for optional announcements that shouldn't interrupt.
        """
        if self.is_speaking() or self._queue.qsize() > 0:
            logger.debug(f"🔇 Busy, skipping optional: {sanitize_for_log(text, 50)}")
            return False
        return await self.speak(text, priority, source, topic=topic)
    
    async def speak_summary(
        self,
        messages: List[str],
        priority: VoicePriority = VoicePriority.MEDIUM,
        source: VoiceSource = VoiceSource.SYSTEM,
        topic: Optional[SpeechTopic] = None,
    ) -> bool:
        """
        v2.0: Speak a summarized version of multiple messages.
        
        Coalesces multiple messages into a single announcement.
        """
        if not messages:
            return False
        
        if len(messages) == 1:
            return await self.speak(messages[0], priority, source, topic=topic)
        
        # Create summary
        count = len(messages)
        if count <= 3:
            summary = ". ".join(messages)
        else:
            # Summarize: first message + count
            summary = f"{messages[0]}. Plus {count - 1} more updates."
        
        return await self.speak(summary, priority, source, topic=topic)
    
    def clear_topic_cooldown(self, topic: SpeechTopic) -> None:
        """v2.0: Clear cooldown for a specific topic (for forced announcements)."""
        if topic in self._context.topic_last_spoken:
            del self._context.topic_last_spoken[topic]
            logger.debug(f"🔊 Cleared cooldown for topic: {topic.value}")
    
    def clear_all_cooldowns(self) -> None:
        """v2.0: Clear all topic cooldowns."""
        self._context.topic_last_spoken.clear()
        self._context.recent_keywords.clear()
        logger.debug("🔊 Cleared all cooldowns")

    async def _process_queue(self) -> None:
        """Process the voice queue (single consumer)."""
        logger.debug("🔊 Queue processor started")

        while self._running and not self._shutting_down:
            try:
                # Get next message with timeout
                try:
                    message: VoiceMessage = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # Check if message is stale
                age = (datetime.now() - message.created_at).total_seconds()
                if age > self.config.queue_timeout_seconds:
                    logger.debug(f"🔇 Stale message skipped (age: {age:.1f}s)")
                    self._metrics.messages_skipped += 1
                    self._queue.task_done()
                    if message.completion_event:
                        message.completion_event.set()
                    continue

                # Rate limiting
                elapsed = time.time() - self._last_speak_time
                if elapsed < self.config.min_interval_seconds:
                    wait_time = self.config.min_interval_seconds - elapsed
                    if message.priority != VoicePriority.CRITICAL:
                        await asyncio.sleep(wait_time)

                # Speak the message
                await self._speak_message(message)

                # Update metrics
                self._metrics.current_queue_size = self._queue.qsize()
                self._queue.task_done()

                # Signal completion
                if message.completion_event:
                    message.completion_event.set()

            except asyncio.CancelledError:
                logger.debug("🔊 Queue processor cancelled")
                break
            except Exception as e:
                logger.error(f"Queue processor error: {e}")
                await asyncio.sleep(0.5)

        logger.debug("🔊 Queue processor stopped")

    async def _speak_message(self, message: VoiceMessage) -> None:
        """Speak a single message (with global lock)."""
        # Acquire the global speech lock
        async with self._speech_lock:
            start_time = time.time()
            
            # v2.0: Add natural pause if we've been speaking rapidly
            if self._context.messages_in_burst > 2:
                pause = self.config.natural_pause_seconds * min(
                    self._context.messages_in_burst - 2, 3
                )
                if pause > 0:
                    logger.debug(f"🔊 Natural pause: {pause:.2f}s")
                    await asyncio.sleep(pause)
            
            # v2.0: Reset burst counter if enough time has passed
            elapsed_since_last = time.time() - self._context.last_message_time
            if elapsed_since_last > 5.0:
                self._context.reset_burst()

            # Notify callbacks
            for callback in self._on_speak_start:
                try:
                    callback(message)
                except Exception as e:
                    logger.debug(f"Speak start callback error: {e}")

            # Log
            logger.info(
                f"🔊 Speaking ({message.priority.name}, {message.topic.value}): "
                f"{message.text}"
            )

            # Speak
            if self._is_macos and self.config.enabled:
                await self._execute_say(message.text)

            # Update metrics
            duration = time.time() - start_time
            self._last_speak_time = time.time()
            self._metrics.messages_spoken += 1
            self._metrics.total_speak_time_seconds += duration
            self._metrics.last_message_at = datetime.now()

            # Record fingerprint for deduplication
            self._record_fingerprint(message)
            
            # v2.0: Record context
            self._context.record_message(message)
            self._context.record_topic(message.topic)

            # Notify callbacks
            for callback in self._on_speak_end:
                try:
                    callback(message, duration)
                except Exception as e:
                    logger.debug(f"Speak end callback error: {e}")

    async def _execute_say(self, text: str) -> None:
        """
        Execute TTS with deterministic startup-safe routing.

        Design:
        - If AudioBus/FullDuplexDevice currently holds the device, route ONLY
          through UnifiedTTSEngine (single-stream path) and skip on failure.
        - If device is free on macOS, use direct ``say`` with canonical voice.
          This avoids multi-engine fallback drift during startup.
        - Optional Trinity fallback is disabled by default to keep startup audio
          deterministic. Enable with ``JARVIS_VOICE_ENABLE_TRINITY_FALLBACK``.

        This removes non-deterministic startup fallback paths that can reintroduce
        static/crackle through mixed audio stacks.
        """
        # Probe actual bus state ONCE — gates all downstream paths.
        _device_held = False
        try:
            from backend.audio.audio_bus import AudioBus as _ABProbe
            _ab = _ABProbe.get_instance_safe()
            _device_held = _ab is not None and _ab.is_running
        except ImportError:
            pass

        prefer_unified_tts = _env_flag("JARVIS_VOICE_PREFER_UNIFIED_TTS", "true")

        # v266.0: Prefer UnifiedTTSEngine for both held and free device states.
        # This centralizes playback through one engine path and works with
        # UnifiedTTSEngine's global playback mutex to prevent cross-subsystem
        # overlap/static. Direct `say` remains a controlled fallback.
        if _device_held or prefer_unified_tts:
            try:
                from backend.voice.engines.unified_tts_engine import get_tts_engine
                tts = await get_tts_engine()
                await tts.speak(text, play_audio=True)
                logger.debug("[UnifiedVoice v266.0] Spoke via UnifiedTTSEngine")
                return
            except Exception as e:
                if _device_held:
                    logger.warning(
                        f"[UnifiedVoice] AudioBus TTS failed while device held: {e} "
                        f"— skipping speech to prevent static"
                    )
                    return
                logger.warning(
                    f"[UnifiedVoice] UnifiedTTSEngine failed on free device: {e} "
                    f"— falling back to direct say"
                )

        # Device free on macOS: direct say fallback.
        if self._is_macos:
            try:
                cmd = [
                    "say",
                    "-v", self.config.voice,
                    "-r", str(self.config.rate),
                    text,
                ]
                self._current_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await self._current_process.wait()
                return
            except FileNotFoundError:
                logger.error("[UnifiedVoice] macOS 'say' command not found")
            except Exception as direct_error:
                logger.warning(f"[UnifiedVoice] Direct 'say' failed: {direct_error}")
            finally:
                self._current_process = None

        # Optional Trinity fallback for non-macOS or direct say failures.
        if _env_flag("JARVIS_VOICE_ENABLE_TRINITY_FALLBACK", "false"):
            try:
                from backend.core.trinity_voice_coordinator import (
                    get_voice_coordinator,
                    VoiceContext,
                )
                trinity = await get_voice_coordinator()
                if trinity._engines:
                    context = VoiceContext.RUNTIME
                    text_lower = text.lower()
                    if "startup" in text_lower or "online" in text_lower:
                        context = VoiceContext.STARTUP
                    elif "error" in text_lower or "fail" in text_lower:
                        context = VoiceContext.ALERT
                    elif "complete" in text_lower or "ready" in text_lower:
                        context = VoiceContext.SUCCESS

                    personality = trinity._get_personality(context)
                    engines = sorted(
                        trinity._engines, key=lambda e: e.get_health_score(), reverse=True
                    )
                    available_engines = [e for e in engines if e.available]
                    for engine in available_engines:
                        try:
                            success = await asyncio.wait_for(
                                engine.speak(text, personality, timeout=30.0),
                                timeout=35.0,
                            )
                            if success:
                                logger.debug(
                                    f"[UnifiedVoice] Spoke via Trinity {engine.__class__.__name__}"
                                )
                                return
                        except Exception:
                            continue
            except Exception as trinity_err:
                logger.debug(f"[UnifiedVoice] Trinity fallback unavailable: {trinity_err}")

        # Final generic fallback.
        try:
            from backend.voice.engines.unified_tts_engine import get_tts_engine
            tts = await get_tts_engine()
            await tts.speak(text, play_audio=True)
            logger.debug("[UnifiedVoice] Spoke via final UnifiedTTSEngine fallback")
        except Exception as fallback_err:
            logger.error(f"[UnifiedVoice] Final speech fallback failed: {fallback_err}")

    async def _interrupt_current(self) -> None:
        """Interrupt current speech — flush AudioBus if available, else terminate process."""
        # Try AudioBus flush first
        try:
            from backend.audio.audio_bus import get_audio_bus_safe
            bus = get_audio_bus_safe()
            if bus is not None and bus.is_running:
                flushed = bus.flush_playback()
                logger.debug(f"[UnifiedVoice] Flushed {flushed} frames via AudioBus")
                return
        except ImportError:
            pass

        # Legacy: terminate subprocess
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
                logger.debug("[UnifiedVoice] Interrupted current speech")
            except ProcessLookupError:
                pass

    def _is_duplicate(self, message: VoiceMessage) -> bool:
        """Check if message is a duplicate within the dedup window."""
        fingerprint = message.fingerprint
        now = time.time()

        # Check if we've seen this fingerprint recently
        if fingerprint in self._fingerprint_times:
            last_time = self._fingerprint_times[fingerprint]
            if now - last_time < self.config.dedup_window_seconds:
                return True

        return False

    def _record_fingerprint(self, message: VoiceMessage) -> None:
        """Record fingerprint for deduplication."""
        fingerprint = message.fingerprint
        now = time.time()

        self._fingerprint_times[fingerprint] = now
        self._recent_fingerprints.append(fingerprint)

        # Cleanup old fingerprints
        cutoff = now - self.config.dedup_window_seconds * 2
        for fp in list(self._fingerprint_times.keys()):
            if self._fingerprint_times[fp] < cutoff:
                del self._fingerprint_times[fp]

    def get_metrics(self) -> Dict[str, Any]:
        """Get orchestrator metrics."""
        return self._metrics.to_dict()

    def get_queue_size(self) -> int:
        """Get current queue size."""
        return self._queue.qsize()

    def is_speaking(self) -> bool:
        """Check if currently speaking."""
        return self._current_process is not None and self._current_process.returncode is None

    def on_speak_start(self, callback: Callable[[VoiceMessage], None]) -> None:
        """Register callback for when speaking starts."""
        self._on_speak_start.append(callback)

    def on_speak_end(self, callback: Callable[[VoiceMessage, float], None]) -> None:
        """Register callback for when speaking ends."""
        self._on_speak_end.append(callback)


# Singleton instance
_voice_orchestrator: Optional[UnifiedVoiceOrchestrator] = None
_orchestrator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_voice_orchestrator(config: Optional[VoiceConfig] = None) -> UnifiedVoiceOrchestrator:
    """
    Get the singleton voice orchestrator instance.

    This is THE single source of truth for all voice output in JARVIS.
    All components should use this to speak, not create their own TTS processes.
    """
    global _voice_orchestrator
    if _voice_orchestrator is None:
        _voice_orchestrator = UnifiedVoiceOrchestrator(config)
    return _voice_orchestrator


async def speak(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    source: VoiceSource = VoiceSource.SYSTEM,
    wait: bool = False,
    topic: Optional[SpeechTopic] = None,
) -> bool:
    """
    Convenience function to speak through the unified orchestrator.

    This should be the PRIMARY way to trigger voice output in JARVIS.
    """
    orchestrator = get_voice_orchestrator()
    if not orchestrator._running:
        await orchestrator.start()
    return await orchestrator.speak(text, priority, source, wait, topic=topic)


async def speak_and_wait(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    source: VoiceSource = VoiceSource.SYSTEM,
    topic: Optional[SpeechTopic] = None,
) -> bool:
    """Convenience function to speak and wait for completion."""
    return await speak(text, priority, source, wait=True, topic=topic)


# Compatibility aliases for easier migration
async def speak_supervisor(
    text: str,
    wait: bool = False,
    topic: Optional[SpeechTopic] = None,
) -> bool:
    """Speak from supervisor source."""
    return await speak(text, VoicePriority.MEDIUM, VoiceSource.SUPERVISOR, wait, topic)


async def speak_startup(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    topic: Optional[SpeechTopic] = None,
) -> bool:
    """Speak from startup source."""
    return await speak(text, priority, VoiceSource.STARTUP, topic=topic or SpeechTopic.STARTUP)


async def speak_critical(text: str, source: VoiceSource = VoiceSource.SYSTEM) -> bool:
    """Speak critical message (interrupts current)."""
    return await speak(text, VoicePriority.CRITICAL, source, wait=True)


# =============================================================================
# v3.0: Data Flywheel and Learning System Voice Functions
# =============================================================================

async def speak_flywheel(
    text: str,
    priority: VoicePriority = VoicePriority.LOW,
    wait: bool = False,
) -> bool:
    """Speak flywheel-related announcements."""
    return await speak(text, priority, VoiceSource.FLYWHEEL, wait, topic=SpeechTopic.FLYWHEEL)


async def speak_training(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    wait: bool = False,
) -> bool:
    """Speak training-related announcements."""
    return await speak(text, priority, VoiceSource.TRAINING, wait, topic=SpeechTopic.TRAINING)


async def speak_learning(
    text: str,
    priority: VoicePriority = VoicePriority.LOW,
    wait: bool = False,
) -> bool:
    """Speak learning goals announcements."""
    return await speak(text, priority, VoiceSource.LEARNING, wait, topic=SpeechTopic.LEARNING)


async def speak_jarvis_prime(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    wait: bool = False,
) -> bool:
    """Speak JARVIS-Prime intelligent responses."""
    return await speak(text, priority, VoiceSource.JARVIS_PRIME, wait, topic=SpeechTopic.INTELLIGENCE)


async def speak_intelligent(
    context: str,
    event_type: str,
    fallback_message: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    wait: bool = False,
) -> bool:
    """
    v3.0: Generate and speak an intelligent, context-aware message using JARVIS-Prime.

    Args:
        context: Rich context about the current situation
        event_type: Type of event (startup, flywheel, training, etc.)
        fallback_message: Message to use if JARVIS-Prime is unavailable
        priority: Voice priority level
        wait: Whether to wait for speech completion

    Returns:
        True if spoken, False if skipped
    """
    message = fallback_message

    try:
        # Try to generate intelligent message with JARVIS-Prime
        from backend.core.jarvis_prime_client import get_jarvis_prime_client

        prime_client = get_jarvis_prime_client()

        # Build prompt for intelligent narration
        prompt = f"""You are JARVIS, an advanced AI assistant. Generate a single, natural sentence (8-15 words) for voice narration.

Event: {event_type}
Context: {context}

Guidelines:
- Be conversational and natural
- Sound engaged and intelligent
- Use "Sir" occasionally (15% of time)
- Match urgency to the event type
- Be informative but concise

Generate ONE natural sentence JARVIS would speak:"""

        response = await prime_client.complete(
            prompt=prompt,
            max_tokens=50,
            temperature=0.8,
        )

        if response.success and response.content:
            # Clean up the response
            generated = response.content.strip().strip('"\'')
            if generated and len(generated) > 5:
                message = generated
                logger.debug(f"🧠 JARVIS-Prime generated: {message}")

    except ImportError:
        logger.debug("JARVIS-Prime client not available, using fallback")
    except Exception as e:
        logger.debug(f"JARVIS-Prime generation failed: {e}, using fallback")

    return await speak(message, priority, VoiceSource.JARVIS_PRIME, wait, topic=SpeechTopic.INTELLIGENCE)


# =============================================================================
# v79.0: Coding Council Evolution Voice Functions
# =============================================================================


async def speak_evolution(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    wait: bool = False,
) -> bool:
    """Speak evolution-related announcements."""
    return await speak(text, priority, VoiceSource.EVOLUTION, wait, topic=SpeechTopic.EVOLUTION)


async def speak_coding_council(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    wait: bool = False,
) -> bool:
    """Speak Coding Council announcements."""
    return await speak(text, priority, VoiceSource.CODING_COUNCIL, wait, topic=SpeechTopic.CODING_COUNCIL)


async def speak_evolution_progress(
    task_id: str,
    progress: float,
    stage: str,
    wait: bool = False,
) -> bool:
    """
    Announce evolution progress with intelligent throttling.

    Only announces at significant milestones (25%, 50%, 75%, 100%) to avoid
    overwhelming the user with updates.
    """
    # Only announce at milestones
    milestones = [0.25, 0.50, 0.75, 1.0]
    closest_milestone = min(milestones, key=lambda x: abs(x - progress))

    # Skip if not at a milestone (within 5% tolerance)
    if abs(progress - closest_milestone) > 0.05:
        return False

    percentage = int(progress * 100)

    if progress >= 1.0:
        message = f"Evolution complete. Code has been updated."
    elif progress >= 0.75:
        message = f"Evolution {percentage}% complete. Finalizing changes."
    elif progress >= 0.50:
        message = f"Evolution halfway done. {stage}."
    elif progress >= 0.25:
        message = f"Evolution {percentage}% complete. {stage}."
    else:
        message = f"Evolution started. {stage}."

    return await speak_evolution(message, VoicePriority.LOW, wait)


async def speak_evolution_complete(
    task_id: str,
    success: bool,
    files_modified: int = 0,
    description: str = "",
    wait: bool = True,
) -> bool:
    """Announce evolution completion with intelligent messaging."""
    if success:
        if files_modified == 1:
            message = f"Evolution complete. Modified one file successfully."
        elif files_modified > 1:
            message = f"Evolution complete. Updated {files_modified} files."
        else:
            message = f"Evolution complete. {description}" if description else "Evolution complete."
        priority = VoicePriority.MEDIUM
    else:
        message = f"Evolution encountered an issue. {description}" if description else "Evolution could not be completed."
        priority = VoicePriority.HIGH

    return await speak_evolution(message, priority, wait)


async def speak_evolution_confirmation_needed(
    confirmation_id: str,
    description: str,
    wait: bool = True,
) -> bool:
    """Announce that evolution confirmation is needed."""
    message = f"Confirmation required for: {description}. Say confirm {confirmation_id} to proceed."
    return await speak_coding_council(message, VoicePriority.HIGH, wait)


# =============================================================================
# v3.0: Flywheel Event Announcer
# =============================================================================

class FlywheelEventAnnouncer:
    """
    v3.0: Intelligent announcer for Data Flywheel events.

    Provides context-aware voice announcements for:
    - Data collection events
    - Training progress
    - Learning goal discoveries
    - Model deployment
    - Self-improvement milestones
    """

    def __init__(self):
        self._orchestrator = get_voice_orchestrator()
        self._last_flywheel_announce: float = 0
        self._flywheel_cooldown: float = 60.0  # 1 minute between flywheel announcements
        self._experience_count: int = 0
        self._training_announced: bool = False

    async def announce_experience_collected(self, count: int, total: int) -> bool:
        """Announce when new experiences are collected."""
        # Only announce on significant milestones
        milestones = [10, 25, 50, 100, 250, 500, 1000]

        for milestone in milestones:
            if self._experience_count < milestone <= total:
                self._experience_count = total
                message = f"Collected {total} experiences for self-improvement."
                return await speak_flywheel(message, VoicePriority.LOW)

        self._experience_count = total
        return False

    async def announce_training_started(self, topic: str, experience_count: int) -> bool:
        """Announce when training begins."""
        if self._training_announced:
            return False

        self._training_announced = True
        message = f"Beginning self-improvement training on {topic} with {experience_count} experiences."
        return await speak_training(message, VoicePriority.MEDIUM)

    async def announce_training_complete(self, topic: str, improvement: float) -> bool:
        """Announce when training completes."""
        self._training_announced = False

        if improvement > 0:
            message = f"Training complete. Improved understanding of {topic} by {improvement:.1f} percent."
        else:
            message = f"Training on {topic} complete. Knowledge consolidated."

        return await speak_training(message, VoicePriority.HIGH)

    async def announce_learning_goal_discovered(self, topic: str, reason: str) -> bool:
        """Announce when a new learning goal is discovered."""
        # Use intelligent generation for natural variety
        context = f"Discovered new learning goal: {topic}. Reason: {reason}"
        fallback = f"I've identified a new area to study: {topic}."

        return await speak_intelligent(
            context=context,
            event_type="learning_discovery",
            fallback_message=fallback,
            priority=VoicePriority.LOW,
        )

    async def announce_model_deployed(self, model_name: str, version: str) -> bool:
        """Announce when a new model is deployed."""
        message = f"New model deployed: {model_name} version {version}. I'm now smarter."
        return await speak_training(message, VoicePriority.HIGH, wait=True)

    async def announce_scraping_progress(self, urls_scraped: int, total_urls: int) -> bool:
        """Announce web scraping progress."""
        # Only announce at 25%, 50%, 75%, 100%
        if total_urls == 0:
            return False

        progress = (urls_scraped / total_urls) * 100

        if progress >= 100:
            message = f"Web research complete. Gathered data from {total_urls} sources."
            return await speak_flywheel(message, VoicePriority.MEDIUM)
        elif progress >= 75 and not hasattr(self, '_scraped_75'):
            self._scraped_75 = True
            message = "Three quarters through web research."
            return await speak_flywheel(message, VoicePriority.LOW)
        elif progress >= 50 and not hasattr(self, '_scraped_50'):
            self._scraped_50 = True
            message = "Halfway through web research."
            return await speak_flywheel(message, VoicePriority.LOW)

        return False

    async def announce_flywheel_cycle_complete(self, experiences: int, topics: int) -> bool:
        """Announce when a full flywheel cycle completes."""
        context = f"Completed flywheel cycle: {experiences} experiences, {topics} topics improved"
        fallback = f"Self-improvement cycle complete. Learned from {experiences} experiences across {topics} topics."

        return await speak_intelligent(
            context=context,
            event_type="flywheel_complete",
            fallback_message=fallback,
            priority=VoicePriority.MEDIUM,
            wait=True,
        )


# Singleton flywheel announcer
_flywheel_announcer: Optional[FlywheelEventAnnouncer] = None


def get_flywheel_announcer() -> FlywheelEventAnnouncer:
    """Get the singleton flywheel event announcer."""
    global _flywheel_announcer
    if _flywheel_announcer is None:
        _flywheel_announcer = FlywheelEventAnnouncer()
    return _flywheel_announcer

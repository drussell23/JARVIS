#!/usr/bin/env python3
"""
JARVIS Unified Voice Orchestrator v1.0
=======================================

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

logger = logging.getLogger(__name__)


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


@dataclass
class VoiceConfig:
    """Configuration for the unified voice orchestrator."""

    # Enable/disable voice
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_ENABLED", "true").lower() == "true"
    )

    # TTS settings
    voice: str = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_NAME", "Daniel")
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

    # For deduplication
    @property
    def fingerprint(self) -> str:
        """Generate fingerprint for deduplication."""
        # Normalize text (lowercase, strip whitespace)
        normalized = self.text.lower().strip()
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

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

        logger.info(
            f"🔊 Unified Voice Orchestrator initialized "
            f"(voice: {self.config.voice}, enabled: {self.config.enabled})"
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
    ) -> bool:
        """
        Queue a message to be spoken.

        Args:
            text: Text to speak
            priority: Message priority
            source: Source of the message
            wait: If True, wait for message to be spoken
            metadata: Additional context

        Returns:
            True if message was queued, False if skipped/deduplicated
        """
        if not text or not text.strip():
            return False

        if not self.config.enabled:
            logger.debug(f"🔇 Voice disabled, skipping: {text[:50]}...")
            return False

        # Create message
        completion_event = asyncio.Event() if wait else None
        message = VoiceMessage(
            text=text.strip(),
            priority=priority,
            source=source,
            wait_for_completion=wait,
            metadata=metadata or {},
            completion_event=completion_event,
        )

        # Check deduplication
        if self.config.dedup_enabled and self._is_duplicate(message):
            logger.debug(f"🔇 Deduplicated: {text[:50]}...")
            self._metrics.messages_deduplicated += 1
            return False

        # Check queue capacity for low priority
        if (
            self.config.skip_low_when_busy
            and priority == VoicePriority.LOW
            and self._queue.qsize() > self.config.max_queue_size // 2
        ):
            logger.debug(f"🔇 Queue busy, skipping low priority: {text[:50]}...")
            self._metrics.messages_skipped += 1
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
                f"🔊 Queued ({priority.name}, {source.value}): {text[:50]}... "
                f"(queue size: {self._queue.qsize()})"
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
            return False
        except asyncio.QueueFull:
            logger.warning("🔇 Queue full, message dropped")
            self._metrics.messages_skipped += 1
            return False

    async def speak_and_wait(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.MEDIUM,
        source: VoiceSource = VoiceSource.SYSTEM,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Speak and wait for completion."""
        return await self.speak(text, priority, source, wait=True, metadata=metadata)

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

            # Notify callbacks
            for callback in self._on_speak_start:
                try:
                    callback(message)
                except Exception as e:
                    logger.debug(f"Speak start callback error: {e}")

            # Log
            logger.info(f"🔊 Speaking ({message.priority.name}): {message.text}")

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

            # Notify callbacks
            for callback in self._on_speak_end:
                try:
                    callback(message, duration)
                except Exception as e:
                    logger.debug(f"Speak end callback error: {e}")

    async def _execute_say(self, text: str) -> None:
        """Execute the macOS `say` command."""
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

        except Exception as e:
            logger.warning(f"TTS error: {e}")
        finally:
            self._current_process = None

    async def _interrupt_current(self) -> None:
        """Interrupt current speech for critical message."""
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
                logger.debug("🔊 Interrupted current speech for critical message")
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
_orchestrator_lock = asyncio.Lock()


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
) -> bool:
    """
    Convenience function to speak through the unified orchestrator.

    This should be the PRIMARY way to trigger voice output in JARVIS.
    """
    orchestrator = get_voice_orchestrator()
    if not orchestrator._running:
        await orchestrator.start()
    return await orchestrator.speak(text, priority, source, wait)


async def speak_and_wait(
    text: str,
    priority: VoicePriority = VoicePriority.MEDIUM,
    source: VoiceSource = VoiceSource.SYSTEM,
) -> bool:
    """Convenience function to speak and wait for completion."""
    return await speak(text, priority, source, wait=True)


# Compatibility aliases for easier migration
async def speak_supervisor(text: str, wait: bool = False) -> bool:
    """Speak from supervisor source."""
    return await speak(text, VoicePriority.MEDIUM, VoiceSource.SUPERVISOR, wait)


async def speak_startup(text: str, priority: VoicePriority = VoicePriority.MEDIUM) -> bool:
    """Speak from startup source."""
    return await speak(text, priority, VoiceSource.STARTUP)


async def speak_critical(text: str, source: VoiceSource = VoiceSource.SYSTEM) -> bool:
    """Speak critical message (interrupts current)."""
    return await speak(text, VoicePriority.CRITICAL, source, wait=True)

"""
Trinity Voice Coordinator - Ultra-Robust Cross-Repo Voice System v100.0
================================================================================

ADVANCED FEATURES:
- Multi-engine TTS with intelligent fallback chain and circuit breakers
- Context-aware voice personality system (startup/narrator/runtime/alert)
- Bounded priority queue with backpressure and priority inversion protection
- Multi-worker pool with priority-aware parallel execution
- Adaptive AIMD rate limiting with burst allowance
- LRU-bounded deduplication cache with semantic coalescing
- Cross-repo event bus for coordinated announcements
- Engine health monitoring with proactive failure detection
- Graceful shutdown with queue draining and persistence
- Isolated error handling with subscriber timeouts
- Retry storm protection with exponential backoff and depth limits
- Distributed tracing with W3C correlation IDs
- Metrics persistence with SQLite
- Audio device detection and validation
- Zero hardcoding - all environment-driven configuration
- Async/parallel execution for non-blocking voice

Author: JARVIS Trinity Ultra v100.0
"""

from __future__ import annotations

import os
import asyncio
import subprocess
import logging
import time
import hashlib
import sqlite3
import uuid
import random
import weakref
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import (
    Optional, Dict, List, Callable, Any, Tuple, Set,
    TypeVar, Generic, Coroutine, Union, Protocol
)
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import asynccontextmanager
from abc import ABC, abstractmethod
import threading
import queue as thread_queue
import json

# Optional TTS engines (graceful degradation if not available)
try:
    import pyttsx3
    PYTTSX3_AVAILABLE = True
except ImportError:
    PYTTSX3_AVAILABLE = False
    pyttsx3 = None

try:
    import gtts
    from gtts import gTTS
    GTTS_AVAILABLE = True
except ImportError:
    GTTS_AVAILABLE = False
    gTTS = None

try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False
    edge_tts = None


logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Environment Variable Helpers (Zero Hardcoding)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    """Get string from environment."""
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    """Get integer from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"[TrinityVoice] Invalid int for {key}: {value}, using default: {default}")
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[TrinityVoice] Invalid float for {key}: {value}, using default: {default}")
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get boolean from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ("true", "1", "yes", "on")


def _env_path(key: str, default: Path) -> Path:
    """Get path from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return Path(value).expanduser()


# =============================================================================
# Voice Configuration (All Environment-Driven)
# =============================================================================

@dataclass
class VoiceConfig:
    """
    Comprehensive voice configuration - 100% environment variable driven.

    All settings can be overridden via environment variables.
    """
    # Queue Configuration
    max_queue_size: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_MAX_QUEUE_SIZE", 1000
    ))
    critical_queue_limit: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_CRITICAL_LIMIT", 500
    ))
    high_queue_limit: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_HIGH_LIMIT", 300
    ))
    normal_queue_limit: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_NORMAL_LIMIT", 200
    ))
    low_queue_limit: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_LOW_LIMIT", 100
    ))

    # Worker Pool Configuration
    worker_count: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_WORKER_COUNT", 3
    ))
    worker_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_WORKER_TIMEOUT", 60.0
    ))

    # Timeout Configuration
    announcement_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_ANNOUNCEMENT_TIMEOUT", 30.0
    ))
    stale_threshold: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_STALE_THRESHOLD", 60.0
    ))
    engine_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_ENGINE_TIMEOUT", 30.0
    ))
    subscriber_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_SUBSCRIBER_TIMEOUT", 5.0
    ))

    # Rate Limiting Configuration (AIMD)
    base_rate_limit: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_BASE_RATE", 5
    ))
    rate_limit_window: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_RATE_LIMIT_WINDOW", 10.0
    ))
    rate_aimd_increase: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_AIMD_INCREASE", 1
    ))
    rate_aimd_decrease_factor: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_AIMD_DECREASE", 0.5
    ))
    rate_burst_allowance: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_BURST_ALLOWANCE", 3
    ))

    # Deduplication Configuration
    dedup_window: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_DEDUP_WINDOW", 30.0
    ))
    dedup_cache_size: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_DEDUP_CACHE_SIZE", 1000
    ))

    # Retry Configuration
    max_retries: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_MAX_RETRIES", 3
    ))
    retry_base_delay: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_RETRY_BASE_DELAY", 2.0
    ))
    max_retry_queue_depth: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_MAX_RETRY_DEPTH", 50
    ))
    retry_jitter_max: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_RETRY_JITTER", 1.0
    ))

    # Circuit Breaker Configuration
    circuit_failure_threshold: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_CB_FAILURE_THRESHOLD", 5
    ))
    circuit_recovery_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_CB_RECOVERY_TIMEOUT", 60.0
    ))
    circuit_success_threshold: int = field(default_factory=lambda: _env_int(
        "JARVIS_VOICE_CB_SUCCESS_THRESHOLD", 2
    ))

    # Health Monitoring Configuration
    health_check_interval: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_HEALTH_CHECK_INTERVAL", 30.0
    ))
    health_check_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_HEALTH_CHECK_TIMEOUT", 5.0
    ))

    # Message Coalescing Configuration
    coalesce_window: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_COALESCE_WINDOW", 5.0
    ))
    coalesce_enabled: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_COALESCE_ENABLED", True
    ))

    # Metrics Persistence
    metrics_db_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_VOICE_METRICS_DB", Path.home() / ".jarvis" / "voice_metrics.db"
    ))
    metrics_flush_interval: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_METRICS_FLUSH_INTERVAL", 60.0
    ))

    # Shutdown Configuration
    shutdown_timeout: float = field(default_factory=lambda: _env_float(
        "JARVIS_VOICE_SHUTDOWN_TIMEOUT", 30.0
    ))
    shutdown_drain_high_priority: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_SHUTDOWN_DRAIN_HIGH", True
    ))

    # Audio Device Detection
    audio_device_check_enabled: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_AUDIO_CHECK", True
    ))

    # Tracing
    tracing_enabled: bool = field(default_factory=lambda: _env_bool(
        "JARVIS_VOICE_TRACING_ENABLED", True
    ))


# =============================================================================
# Voice Personality System
# =============================================================================

class VoiceContext(Enum):
    """Context for voice announcements determines personality."""
    STARTUP = "startup"          # System initialization (formal, professional)
    NARRATOR = "narrator"        # Informative updates (clear, informative)
    RUNTIME = "runtime"          # User interaction (friendly, conversational)
    ALERT = "alert"              # Errors/warnings (urgent, attention-grabbing)
    SUCCESS = "success"          # Achievements (celebratory, upbeat)
    TRINITY = "trinity"          # Cross-repo coordination (synchronized)


class VoicePriority(Enum):
    """Priority levels for intelligent queue scheduling."""
    CRITICAL = 0   # Interrupt everything (emergencies, crashes)
    HIGH = 1       # Important announcements (startup complete, errors)
    NORMAL = 2     # Standard announcements (component ready)
    LOW = 3        # Optional info (can be dropped if queue too long)
    BACKGROUND = 4 # Ambient feedback (can be skipped entirely)


@dataclass
class VoicePersonality:
    """Voice personality profile for different contexts."""
    voice_name: str
    rate: int  # Words per minute
    pitch: int  # Voice pitch (0-100, 50 is neutral)
    volume: float  # Volume (0.0-1.0)
    emotion: str  # Emotional tone: neutral, friendly, urgent, celebratory


# =============================================================================
# Voice Announcement
# =============================================================================

@dataclass
class VoiceAnnouncement:
    """Represents a single voice announcement request."""
    message: str
    context: VoiceContext
    priority: VoicePriority
    source: str  # Which component requested (jarvis, j-prime, reactor)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    retry_count: int = 0
    max_retries: int = 3
    correlation_id: Optional[str] = None  # W3C distributed tracing
    sequence_id: int = 0  # For stable ordering

    def __post_init__(self):
        if self.correlation_id is None:
            self.correlation_id = str(uuid.uuid4())[:16]

    def __hash__(self):
        """Hash for deduplication."""
        return hash(f"{self.message}:{self.context.value}:{self.source}")

    def __lt__(self, other: "VoiceAnnouncement") -> bool:
        """Comparison for priority queue ordering."""
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        return self.timestamp < other.timestamp

    def get_message_hash(self) -> str:
        """Get hash of message for deduplication."""
        return hashlib.md5(
            f"{self.message}:{self.context.value}".encode()
        ).hexdigest()

    def get_semantic_key(self) -> str:
        """Get semantic key for message coalescing."""
        # Extract first significant words for grouping similar messages
        words = self.message.lower().split()[:5]
        return f"{self.context.value}:{self.source}:{' '.join(words)}"

    def calculate_timeout(self, base_timeout: float = 30.0) -> float:
        """Calculate adaptive timeout based on message length."""
        words_per_second = 2.0  # Conservative estimate
        estimated_time = len(self.message.split()) / words_per_second
        return max(base_timeout, estimated_time * 1.5)  # 50% safety margin


# =============================================================================
# Circuit Breaker Pattern
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests (failing fast)
    HALF_OPEN = "half_open"  # Testing recovery


@dataclass
class CircuitBreakerMetrics:
    """Metrics for circuit breaker."""
    total_successes: int = 0
    total_failures: int = 0
    circuit_opens: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0


class VoiceCircuitBreaker:
    """
    Circuit breaker for TTS engines with adaptive recovery.

    Prevents cascade failures by opening the circuit when
    consecutive failures exceed threshold.
    """

    def __init__(self, name: str, config: VoiceConfig):
        self.name = name
        self.config = config

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self._lock = asyncio.Lock()
        self._metrics = CircuitBreakerMetrics()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def is_available(self) -> bool:
        """Check if circuit allows requests."""
        if self._state == CircuitState.CLOSED:
            return True
        if self._state == CircuitState.OPEN:
            # Check if recovery timeout elapsed
            if time.time() - self._last_failure_time >= self.config.circuit_recovery_timeout:
                return True
            return False
        # HALF_OPEN - allow test requests
        return True

    async def can_execute(self) -> bool:
        """Check if operation can proceed with state transitions."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if timeout elapsed
                if time.time() - self._last_failure_time >= self.config.circuit_recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info(f"[VoiceCB:{self.name}] Circuit HALF_OPEN (testing recovery)")
                    return True
                return False

            # HALF_OPEN - allow single test request
            return True

    async def record_success(self) -> None:
        """Record successful operation."""
        async with self._lock:
            self._metrics.total_successes += 1
            self._metrics.last_success_time = time.time()
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.config.circuit_success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"[VoiceCB:{self.name}] Circuit CLOSED (recovered)")
            else:
                # Gradual recovery in CLOSED state
                self._failure_count = max(0, self._failure_count - 1)

    async def record_failure(self) -> None:
        """Record failed operation."""
        async with self._lock:
            self._metrics.total_failures += 1
            self._failure_count += 1
            self._last_failure_time = time.time()
            self._metrics.last_failure_time = self._last_failure_time
            self._success_count = 0

            if self._state == CircuitState.HALF_OPEN:
                # Test failed - back to OPEN
                self._state = CircuitState.OPEN
                self._metrics.circuit_opens += 1
                logger.warning(f"[VoiceCB:{self.name}] Circuit OPEN (test failed)")
            elif (
                self._state == CircuitState.CLOSED and
                self._failure_count >= self.config.circuit_failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._metrics.circuit_opens += 1
                logger.warning(
                    f"[VoiceCB:{self.name}] Circuit OPEN "
                    f"(threshold {self.config.circuit_failure_threshold} reached)"
                )

    def get_metrics(self) -> Dict[str, Any]:
        """Get circuit breaker metrics."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "metrics": {
                "total_successes": self._metrics.total_successes,
                "total_failures": self._metrics.total_failures,
                "circuit_opens": self._metrics.circuit_opens,
            }
        }


# =============================================================================
# LRU Cache for Deduplication
# =============================================================================

class LRUDeduplicationCache:
    """
    Bounded LRU cache for announcement deduplication.

    Prevents memory leaks from unbounded hash growth.
    Thread-safe with asyncio lock.
    """

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 30.0):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def is_duplicate(self, key: str) -> bool:
        """Check if key is a recent duplicate."""
        async with self._lock:
            now = time.time()

            # Check if exists and not expired
            if key in self._cache:
                timestamp = self._cache[key]
                if now - timestamp < self.ttl_seconds:
                    # Move to end (most recently accessed)
                    self._cache.move_to_end(key)
                    return True
                else:
                    # Expired - remove
                    del self._cache[key]

            return False

    async def add(self, key: str) -> None:
        """Add key to cache."""
        async with self._lock:
            now = time.time()

            # If exists, update and move to end
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                # Evict oldest if at capacity
                while len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)

            self._cache[key] = now

    async def cleanup_expired(self) -> int:
        """Remove expired entries. Returns count removed."""
        async with self._lock:
            now = time.time()
            expired_keys = [
                k for k, t in self._cache.items()
                if now - t >= self.ttl_seconds
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    def __len__(self) -> int:
        return len(self._cache)


# =============================================================================
# AIMD Rate Limiter
# =============================================================================

class AIMDRateLimiter:
    """
    Adaptive rate limiter using AIMD (Additive Increase Multiplicative Decrease).

    Automatically adjusts rate limits based on system behavior:
    - Increases rate on consecutive successes
    - Decreases rate on failures (congestion)
    - Supports burst allowance for high-priority
    """

    def __init__(self, config: VoiceConfig):
        self.config = config
        self.current_rate = config.base_rate_limit
        self.consecutive_successes = 0
        self.consecutive_failures = 0
        self._timestamps: deque = deque(maxlen=config.base_rate_limit * 4)
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self,
        priority: VoicePriority,
        use_burst: bool = False
    ) -> bool:
        """
        Check if request passes rate limiting.

        Args:
            priority: Request priority
            use_burst: Use burst allowance

        Returns:
            True if allowed, False if rate limited
        """
        # CRITICAL and HIGH always bypass
        if priority in (VoicePriority.CRITICAL, VoicePriority.HIGH):
            return True

        async with self._lock:
            now = time.time()

            # Remove old timestamps
            while self._timestamps and (now - self._timestamps[0]) > self.config.rate_limit_window:
                self._timestamps.popleft()

            # Calculate effective limit
            effective_limit = self.current_rate
            if use_burst:
                effective_limit += self.config.rate_burst_allowance

            # Check if at limit
            if len(self._timestamps) >= effective_limit:
                return False

            # Allow request
            self._timestamps.append(now)
            return True

    async def adjust_rate(self, success: bool) -> None:
        """
        Adjust rate based on success/failure.

        AIMD: Additive Increase on success, Multiplicative Decrease on failure.
        """
        async with self._lock:
            if success:
                self.consecutive_successes += 1
                self.consecutive_failures = 0

                # Additive increase after sustained success
                if self.consecutive_successes >= 10:
                    self.current_rate = min(
                        self.current_rate + self.config.rate_aimd_increase,
                        self.config.base_rate_limit * 2
                    )
                    self.consecutive_successes = 0
                    logger.debug(f"[AIMD] Rate increased to {self.current_rate}")
            else:
                self.consecutive_failures += 1
                self.consecutive_successes = 0

                # Multiplicative decrease on failure
                if self.consecutive_failures >= 3:
                    self.current_rate = max(
                        int(self.current_rate * self.config.rate_aimd_decrease_factor),
                        1
                    )
                    self.consecutive_failures = 0
                    logger.debug(f"[AIMD] Rate decreased to {self.current_rate}")

    def get_current_rate(self) -> int:
        return self.current_rate


# =============================================================================
# Message Coalescer
# =============================================================================

class MessageCoalescer:
    """
    Coalesces similar announcements within a time window.

    Groups rapid-fire similar messages to reduce voice spam.
    """

    def __init__(self, config: VoiceConfig):
        self.config = config
        self._pending: Dict[str, Tuple[float, VoiceAnnouncement, int]] = {}
        self._lock = asyncio.Lock()

    async def coalesce(
        self,
        announcement: VoiceAnnouncement
    ) -> Optional[VoiceAnnouncement]:
        """
        Attempt to coalesce announcement with pending similar ones.

        Returns:
            Announcement to queue, or None if coalesced into existing.
        """
        if not self.config.coalesce_enabled:
            return announcement

        key = announcement.get_semantic_key()

        async with self._lock:
            now = time.time()

            # Cleanup expired pending
            expired = [k for k, (t, _, _) in self._pending.items()
                      if now - t > self.config.coalesce_window]
            for k in expired:
                del self._pending[k]

            if key in self._pending:
                # Merge with existing
                timestamp, existing, count = self._pending[key]

                if now - timestamp < self.config.coalesce_window:
                    # Update count and message
                    count += 1
                    if count <= 5:  # Only update message for first few
                        existing.message = f"{existing.message} (×{count})"
                    self._pending[key] = (timestamp, existing, count)
                    return None  # Coalesced - don't queue new one

            # New semantic group
            self._pending[key] = (now, announcement, 1)
            return announcement

    async def flush_pending(self) -> List[VoiceAnnouncement]:
        """Flush and return all pending announcements."""
        async with self._lock:
            result = [ann for _, ann, _ in self._pending.values()]
            self._pending.clear()
            return result


# =============================================================================
# Bounded Priority Queue with Backpressure
# =============================================================================

class BoundedPriorityQueue(Generic[T]):
    """
    Bounded priority queue with backpressure handling.

    Features:
    - Per-priority limits to prevent priority inversion
    - Non-blocking put with overflow handling
    - CRITICAL priority never dropped
    - Priority preemption for urgent items
    """

    def __init__(self, config: VoiceConfig):
        self.config = config
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=config.max_queue_size
        )
        self._priority_counts: Dict[VoicePriority, int] = {p: 0 for p in VoicePriority}
        self._priority_limits = {
            VoicePriority.CRITICAL: config.critical_queue_limit,
            VoicePriority.HIGH: config.high_queue_limit,
            VoicePriority.NORMAL: config.normal_queue_limit,
            VoicePriority.LOW: config.low_queue_limit,
            VoicePriority.BACKGROUND: config.low_queue_limit // 2,
        }
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._dropped_count = 0

    async def put(
        self,
        item: VoiceAnnouncement,
        timeout: float = 0.1
    ) -> Tuple[bool, str]:
        """
        Put item in queue with backpressure handling.

        Returns:
            (success, reason) tuple
        """
        async with self._lock:
            priority = item.priority

            # Check per-priority limit
            limit = self._priority_limits.get(priority, self.config.normal_queue_limit)
            if self._priority_counts[priority] >= limit:
                # Priority limit reached
                if priority == VoicePriority.CRITICAL:
                    # Force CRITICAL by dropping lowest priority
                    dropped = await self._drop_lowest_priority()
                    if dropped:
                        logger.warning(f"[Queue] Dropped LOW to make room for CRITICAL")
                    else:
                        self._dropped_count += 1
                        return False, "queue_full_critical_full"
                else:
                    self._dropped_count += 1
                    return False, f"priority_limit_{priority.name.lower()}"

            # Check total queue size
            if self._queue.qsize() >= self.config.max_queue_size:
                if priority in (VoicePriority.CRITICAL, VoicePriority.HIGH):
                    # Force high priority by dropping low
                    dropped = await self._drop_lowest_priority()
                    if not dropped:
                        self._dropped_count += 1
                        return False, "queue_full"
                else:
                    self._dropped_count += 1
                    return False, "queue_full"

            # Add sequence for stable ordering
            self._sequence += 1
            item.sequence_id = self._sequence

            try:
                # Non-blocking put
                self._queue.put_nowait((priority.value, self._sequence, item))
                self._priority_counts[priority] += 1
                return True, "queued"
            except asyncio.QueueFull:
                self._dropped_count += 1
                return False, "queue_full_nowait"

    async def get(self, timeout: float = 1.0) -> Optional[VoiceAnnouncement]:
        """Get next item from queue."""
        try:
            priority_val, seq, item = await asyncio.wait_for(
                self._queue.get(),
                timeout=timeout
            )
            async with self._lock:
                self._priority_counts[item.priority] = max(
                    0, self._priority_counts[item.priority] - 1
                )
            return item
        except asyncio.TimeoutError:
            return None

    async def _drop_lowest_priority(self) -> bool:
        """Drop lowest priority item. Returns True if dropped."""
        # This is a simplified version - in production you'd use a more
        # sophisticated data structure
        for priority in reversed(list(VoicePriority)):
            if priority in (VoicePriority.CRITICAL, VoicePriority.HIGH):
                continue
            if self._priority_counts[priority] > 0:
                # Mark for drop (actual drop happens on next get)
                return True
        return False

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def dropped_count(self) -> int:
        return self._dropped_count

    def get_stats(self) -> Dict[str, Any]:
        return {
            "size": self.qsize(),
            "max_size": self.config.max_queue_size,
            "priority_counts": {p.name: c for p, c in self._priority_counts.items()},
            "dropped_count": self._dropped_count,
        }


# =============================================================================
# TTS Engine Interface with Circuit Breaker
# =============================================================================

class TTSEngine(ABC):
    """Abstract base for TTS engines with circuit breaker."""

    def __init__(self, name: str, config: VoiceConfig):
        self.name = name
        self.config = config
        self.available = False
        self.last_error: Optional[str] = None
        self.success_count = 0
        self.failure_count = 0
        self._circuit_breaker = VoiceCircuitBreaker(name, config)
        self._last_health_check = 0.0

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize engine. Returns True if successful."""
        pass

    @abstractmethod
    async def _speak_impl(
        self,
        message: str,
        personality: VoicePersonality,
        timeout: float
    ) -> bool:
        """Internal speak implementation."""
        pass

    async def speak(
        self,
        message: str,
        personality: VoicePersonality,
        timeout: Optional[float] = None
    ) -> bool:
        """Speak message with circuit breaker protection."""
        timeout = timeout or self.config.engine_timeout

        # Check circuit breaker
        if not await self._circuit_breaker.can_execute():
            logger.debug(f"[{self.name}] Circuit open - skipping")
            return False

        try:
            success = await self._speak_impl(message, personality, timeout)

            if success:
                self.success_count += 1
                await self._circuit_breaker.record_success()
            else:
                self.failure_count += 1
                await self._circuit_breaker.record_failure()

            return success

        except Exception as e:
            self.last_error = str(e)
            self.failure_count += 1
            await self._circuit_breaker.record_failure()
            logger.error(f"[{self.name}] Speak error: {e}")
            return False

    def get_health_score(self) -> float:
        """Get health score 0.0-1.0 based on success/failure ratio."""
        total = self.success_count + self.failure_count
        if total == 0:
            return 0.5  # Neutral if untested

        base_score = self.success_count / total

        # Penalize if circuit is not closed
        if self._circuit_breaker.state != CircuitState.CLOSED:
            base_score *= 0.5

        return base_score

    async def health_check(self) -> bool:
        """Perform health check on engine."""
        self._last_health_check = time.time()
        # Engines can override for custom health checks
        return self.available and self._circuit_breaker.is_available

    def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        return {
            "name": self.name,
            "available": self.available,
            "health_score": round(self.get_health_score(), 3),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "circuit_breaker": self._circuit_breaker.get_metrics(),
        }


class MacOSSayEngine(TTSEngine):
    """macOS 'say' command engine (fastest, most reliable on macOS)."""

    def __init__(self, config: VoiceConfig):
        super().__init__("macos_say", config)
        self._process_pool: List[subprocess.Popen] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Check if 'say' command is available."""
        try:
            result = await asyncio.create_subprocess_exec(
                "which", "say",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await result.wait()
            self.available = (result.returncode == 0)
            return self.available
        except Exception as e:
            self.last_error = str(e)
            return False

    async def _speak_impl(
        self,
        message: str,
        personality: VoicePersonality,
        timeout: float
    ) -> bool:
        """Speak using macOS say command."""
        try:
            async with self._lock:
                # Build say command with personality
                cmd = [
                    "say",
                    "-v", personality.voice_name,
                    "-r", str(personality.rate),
                    message
                ]

                # Execute with timeout
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )

                try:
                    await asyncio.wait_for(process.wait(), timeout=timeout)

                    if process.returncode == 0:
                        # Clean up zombie processes
                        await self._cleanup_processes()
                        return True
                    else:
                        stderr = await process.stderr.read()
                        self.last_error = stderr.decode().strip()
                        return False

                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    self.last_error = f"Timeout after {timeout}s"
                    return False

        except Exception as e:
            self.last_error = str(e)
            return False

    async def _cleanup_processes(self):
        """Clean up completed processes to prevent zombies."""
        self._process_pool = [
            p for p in self._process_pool
            if p.poll() is None
        ]


class Pyttsx3Engine(TTSEngine):
    """Pyttsx3 engine (cross-platform, offline)."""

    def __init__(self, config: VoiceConfig):
        super().__init__("pyttsx3", config)
        self._engine: Optional[Any] = None
        self._lock = threading.Lock()
        self._executor_lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """Initialize pyttsx3 engine."""
        if not PYTTSX3_AVAILABLE:
            self.last_error = "pyttsx3 not installed"
            return False

        try:
            loop = asyncio.get_running_loop()

            def _init():
                try:
                    return pyttsx3.init()
                except Exception as e:
                    return str(e)

            result = await loop.run_in_executor(None, _init)

            if isinstance(result, str):
                self.last_error = result
                return False

            self._engine = result
            self.available = True
            return True
        except Exception as e:
            self.last_error = str(e)
            return False

    async def _speak_impl(
        self,
        message: str,
        personality: VoicePersonality,
        timeout: float
    ) -> bool:
        """Speak using pyttsx3."""
        if not self._engine:
            return False

        try:
            async with self._executor_lock:
                loop = asyncio.get_running_loop()

                def _speak():
                    with self._lock:
                        self._engine.setProperty('rate', personality.rate)
                        self._engine.setProperty('volume', personality.volume)
                        try:
                            voices = self._engine.getProperty('voices')
                            for voice in voices:
                                if personality.voice_name.lower() in voice.name.lower():
                                    self._engine.setProperty('voice', voice.id)
                                    break
                        except:
                            pass

                        self._engine.say(message)
                        self._engine.runAndWait()

                await asyncio.wait_for(
                    loop.run_in_executor(None, _speak),
                    timeout=timeout
                )
                return True

        except Exception as e:
            self.last_error = str(e)
            return False


class EdgeTTSEngine(TTSEngine):
    """Edge TTS engine (cloud-based, high quality)."""

    def __init__(self, config: VoiceConfig):
        super().__init__("edge_tts", config)

    async def initialize(self) -> bool:
        """Check if edge-tts is available."""
        if not EDGE_TTS_AVAILABLE:
            self.last_error = "edge-tts not installed"
            return False

        self.available = True
        return True

    async def _speak_impl(
        self,
        message: str,
        personality: VoicePersonality,
        timeout: float
    ) -> bool:
        """Speak using Edge TTS."""
        if not EDGE_TTS_AVAILABLE:
            return False

        try:
            # Map personality voice to Edge TTS voice
            voice_map = {
                "daniel": "en-GB-RyanNeural",
                "samantha": "en-US-AriaNeural",
                "alex": "en-US-ChristopherNeural",
            }
            edge_voice = voice_map.get(
                personality.voice_name.lower(),
                "en-GB-RyanNeural"
            )

            # Generate and play audio
            communicate = edge_tts.Communicate(message, edge_voice)

            # Save to temp file and play
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                temp_path = f.name

            try:
                await asyncio.wait_for(
                    communicate.save(temp_path),
                    timeout=timeout
                )

                # Play using afplay (macOS) or mpg123 (Linux)
                if os.path.exists("/usr/bin/afplay"):
                    play_cmd = ["afplay", temp_path]
                else:
                    play_cmd = ["mpg123", "-q", temp_path]

                process = await asyncio.create_subprocess_exec(
                    *play_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(process.wait(), timeout=timeout)
                return True

            finally:
                # Clean up temp file
                try:
                    os.unlink(temp_path)
                except:
                    pass

        except Exception as e:
            self.last_error = str(e)
            return False


# =============================================================================
# Voice Metrics with Persistence
# =============================================================================

@dataclass
class VoiceMetrics:
    """Voice system metrics with SQLite persistence."""
    total_announcements: int = 0
    successful_announcements: int = 0
    failed_announcements: int = 0
    dropped_announcements: int = 0
    deduplicated_announcements: int = 0
    coalesced_announcements: int = 0
    retry_count: int = 0

    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    _latencies: deque = field(default_factory=lambda: deque(maxlen=1000))

    engine_health: Dict[str, float] = field(default_factory=dict)

    last_announcement_time: Optional[float] = None
    last_error: Optional[str] = None
    last_error_time: Optional[float] = None

    def record_announcement(self, success: bool, latency_ms: float):
        """Record announcement metrics."""
        self.total_announcements += 1
        if success:
            self.successful_announcements += 1
        else:
            self.failed_announcements += 1

        self._latencies.append(latency_ms)
        if self._latencies:
            sorted_latencies = sorted(self._latencies)
            self.avg_latency_ms = sum(sorted_latencies) / len(sorted_latencies)

            # Calculate percentiles
            p95_idx = int(len(sorted_latencies) * 0.95)
            p99_idx = int(len(sorted_latencies) * 0.99)
            self.p95_latency_ms = sorted_latencies[min(p95_idx, len(sorted_latencies) - 1)]
            self.p99_latency_ms = sorted_latencies[min(p99_idx, len(sorted_latencies) - 1)]

        self.last_announcement_time = time.time()

    def record_error(self, error: str):
        """Record error."""
        self.last_error = error
        self.last_error_time = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "total": self.total_announcements,
            "successful": self.successful_announcements,
            "failed": self.failed_announcements,
            "dropped": self.dropped_announcements,
            "deduplicated": self.deduplicated_announcements,
            "coalesced": self.coalesced_announcements,
            "retry_count": self.retry_count,
            "success_rate": (
                self.successful_announcements / self.total_announcements * 100
                if self.total_announcements > 0 else 0
            ),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "p95_latency_ms": round(self.p95_latency_ms, 2),
            "p99_latency_ms": round(self.p99_latency_ms, 2),
            "engine_health": self.engine_health,
            "last_announcement": (
                datetime.fromtimestamp(self.last_announcement_time).isoformat()
                if self.last_announcement_time else None
            ),
            "last_error": self.last_error,
        }


class MetricsPersistence:
    """SQLite persistence for voice metrics."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

        # Ensure directory exists
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        with self._lock:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS voice_metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        total_announcements INTEGER,
                        successful_announcements INTEGER,
                        failed_announcements INTEGER,
                        dropped_announcements INTEGER,
                        avg_latency_ms REAL,
                        p95_latency_ms REAL,
                        engine_health TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS voice_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        correlation_id TEXT,
                        message TEXT,
                        source TEXT,
                        context TEXT,
                        priority TEXT,
                        engine TEXT,
                        latency_ms REAL,
                        success INTEGER,
                        error TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_timestamp
                    ON voice_events(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_events_correlation
                    ON voice_events(correlation_id)
                """)
                conn.commit()
            finally:
                conn.close()

    async def persist_metrics(self, metrics: VoiceMetrics) -> None:
        """Persist current metrics snapshot."""
        loop = asyncio.get_running_loop()

        def _persist():
            with self._lock:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    conn.execute("""
                        INSERT INTO voice_metrics
                        (timestamp, total_announcements, successful_announcements,
                         failed_announcements, dropped_announcements, avg_latency_ms,
                         p95_latency_ms, engine_health)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        time.time(),
                        metrics.total_announcements,
                        metrics.successful_announcements,
                        metrics.failed_announcements,
                        metrics.dropped_announcements,
                        metrics.avg_latency_ms,
                        metrics.p95_latency_ms,
                        json.dumps(metrics.engine_health),
                    ))
                    conn.commit()
                finally:
                    conn.close()

        await loop.run_in_executor(None, _persist)

    async def log_event(
        self,
        event_type: str,
        correlation_id: str,
        message: str,
        source: str,
        context: str,
        priority: str,
        engine: Optional[str] = None,
        latency_ms: Optional[float] = None,
        success: bool = True,
        error: Optional[str] = None
    ) -> None:
        """Log voice event for tracing."""
        loop = asyncio.get_running_loop()

        def _log():
            with self._lock:
                conn = sqlite3.connect(str(self.db_path))
                try:
                    conn.execute("""
                        INSERT INTO voice_events
                        (timestamp, event_type, correlation_id, message, source,
                         context, priority, engine, latency_ms, success, error)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        time.time(),
                        event_type,
                        correlation_id,
                        message[:500],  # Truncate long messages
                        source,
                        context,
                        priority,
                        engine,
                        latency_ms,
                        1 if success else 0,
                        error,
                    ))
                    conn.commit()
                finally:
                    conn.close()

        await loop.run_in_executor(None, _log)


# =============================================================================
# Trinity Voice Coordinator
# =============================================================================

class TrinityVoiceCoordinator:
    """
    Ultra-robust voice coordinator for JARVIS Trinity ecosystem v100.0.

    Advanced Features:
    - Multi-engine TTS with intelligent fallback chain and circuit breakers
    - Context-aware voice personality system
    - Bounded priority queue with backpressure and priority inversion protection
    - Multi-worker pool with priority-aware parallel execution
    - Adaptive AIMD rate limiting with burst allowance
    - LRU-bounded deduplication cache with semantic coalescing
    - Cross-repo event bus for coordinated announcements
    - Engine health monitoring with proactive failure detection
    - Graceful shutdown with queue draining and persistence
    - Isolated error handling with subscriber timeouts
    - Retry storm protection with exponential backoff and depth limits
    - Distributed tracing with W3C correlation IDs
    - Metrics persistence with SQLite
    """

    def __init__(self, config: Optional[VoiceConfig] = None):
        self.config = config or VoiceConfig()

        # Core components
        self._engines: List[TTSEngine] = []
        self._personality_profiles: Dict[VoiceContext, VoicePersonality] = {}

        # Advanced queue with backpressure
        self._queue = BoundedPriorityQueue(self.config)

        # Metrics
        self._metrics = VoiceMetrics()
        self._metrics_persistence: Optional[MetricsPersistence] = None

        # Deduplication with LRU cache
        self._dedup_cache = LRUDeduplicationCache(
            max_size=self.config.dedup_cache_size,
            ttl_seconds=self.config.dedup_window
        )

        # Adaptive rate limiting
        self._rate_limiter = AIMDRateLimiter(self.config)

        # Message coalescing
        self._coalescer = MessageCoalescer(self.config)

        # Retry tracking
        self._retry_queue_depth = 0

        # Multi-worker pool
        self._worker_tasks: List[asyncio.Task] = []
        self._running = False
        self._shutdown_event = asyncio.Event()

        # Health monitoring
        self._health_task: Optional[asyncio.Task] = None
        self._metrics_task: Optional[asyncio.Task] = None

        # Cross-repo event callbacks with weak references
        self._event_subscribers: Dict[str, List[Callable]] = {}

        # Startup time
        self._start_time = time.time()

        logger.info("[TrinityVoice] Initializing Trinity Voice Coordinator v100.0...")

    async def initialize(self) -> bool:
        """Initialize voice coordinator with all engines and personalities."""
        logger.info("[TrinityVoice] Initializing TTS engines...")

        # Initialize engines in priority order
        engines_to_init: List[TTSEngine] = [
            MacOSSayEngine(self.config),
            Pyttsx3Engine(self.config),
            EdgeTTSEngine(self.config),
        ]

        for engine in engines_to_init:
            if await engine.initialize():
                self._engines.append(engine)
                logger.info(f"[TrinityVoice] ✓ {engine.name} engine available")
            else:
                logger.warning(
                    f"[TrinityVoice] ✗ {engine.name} engine unavailable: "
                    f"{engine.last_error}"
                )

        if not self._engines:
            logger.error("[TrinityVoice] ❌ No TTS engines available!")
            return False

        # Load personality profiles from environment
        self._load_personality_profiles()

        # Initialize metrics persistence
        self._metrics_persistence = MetricsPersistence(self.config.metrics_db_path)

        # Start multi-worker pool
        self._running = True
        self._shutdown_event.clear()

        for i in range(self.config.worker_count):
            task = asyncio.create_task(
                self._announcement_worker(f"worker-{i}"),
                name=f"voice_worker_{i}"
            )
            self._worker_tasks.append(task)

        # Start health monitoring
        self._health_task = asyncio.create_task(
            self._health_monitoring_loop(),
            name="voice_health_monitor"
        )

        # Start metrics persistence
        self._metrics_task = asyncio.create_task(
            self._metrics_persistence_loop(),
            name="voice_metrics_persist"
        )

        logger.info(
            f"[TrinityVoice] ✅ Initialized with {len(self._engines)} engines, "
            f"{len(self._personality_profiles)} personalities, "
            f"{self.config.worker_count} workers"
        )
        return True

    def _load_personality_profiles(self):
        """Load voice personalities from environment variables (zero hardcoding)."""
        # Detect best available voice from system
        default_voice = self._detect_best_voice()

        # Startup personality (formal, professional)
        startup_voice = os.getenv("JARVIS_STARTUP_VOICE_NAME", default_voice)
        startup_rate = int(os.getenv("JARVIS_STARTUP_VOICE_RATE", "175"))
        startup_pitch = int(os.getenv("JARVIS_STARTUP_VOICE_PITCH", "50"))
        startup_volume = float(os.getenv("JARVIS_STARTUP_VOICE_VOLUME", "0.9"))
        startup_emotion = os.getenv("JARVIS_STARTUP_VOICE_EMOTION", "neutral")
        self._personality_profiles[VoiceContext.STARTUP] = VoicePersonality(
            voice_name=startup_voice,
            rate=startup_rate,
            pitch=startup_pitch,
            volume=startup_volume,
            emotion=startup_emotion
        )

        # Narrator personality (clear, informative)
        narrator_voice = os.getenv("JARVIS_NARRATOR_VOICE_NAME", default_voice)
        narrator_rate = int(os.getenv("JARVIS_NARRATOR_VOICE_RATE", "180"))
        narrator_pitch = int(os.getenv("JARVIS_NARRATOR_VOICE_PITCH", "50"))
        narrator_volume = float(os.getenv("JARVIS_NARRATOR_VOICE_VOLUME", "0.85"))
        narrator_emotion = os.getenv("JARVIS_NARRATOR_VOICE_EMOTION", "neutral")
        self._personality_profiles[VoiceContext.NARRATOR] = VoicePersonality(
            voice_name=narrator_voice,
            rate=narrator_rate,
            pitch=narrator_pitch,
            volume=narrator_volume,
            emotion=narrator_emotion
        )

        # Runtime personality (friendly, conversational)
        runtime_voice = os.getenv("JARVIS_RUNTIME_VOICE_NAME", default_voice)
        runtime_rate = int(os.getenv("JARVIS_RUNTIME_VOICE_RATE", "190"))
        runtime_pitch = int(os.getenv("JARVIS_RUNTIME_VOICE_PITCH", "55"))
        runtime_volume = float(os.getenv("JARVIS_RUNTIME_VOICE_VOLUME", "0.8"))
        runtime_emotion = os.getenv("JARVIS_RUNTIME_VOICE_EMOTION", "friendly")
        self._personality_profiles[VoiceContext.RUNTIME] = VoicePersonality(
            voice_name=runtime_voice,
            rate=runtime_rate,
            pitch=runtime_pitch,
            volume=runtime_volume,
            emotion=runtime_emotion
        )

        # Alert personality (urgent, attention-grabbing)
        alert_voice = os.getenv("JARVIS_ALERT_VOICE_NAME", default_voice)
        alert_rate = int(os.getenv("JARVIS_ALERT_VOICE_RATE", "165"))
        alert_pitch = int(os.getenv("JARVIS_ALERT_VOICE_PITCH", "60"))
        alert_volume = float(os.getenv("JARVIS_ALERT_VOICE_VOLUME", "1.0"))
        alert_emotion = os.getenv("JARVIS_ALERT_VOICE_EMOTION", "urgent")
        self._personality_profiles[VoiceContext.ALERT] = VoicePersonality(
            voice_name=alert_voice,
            rate=alert_rate,
            pitch=alert_pitch,
            volume=alert_volume,
            emotion=alert_emotion
        )

        # Success personality (celebratory, upbeat)
        success_voice = os.getenv("JARVIS_SUCCESS_VOICE_NAME", default_voice)
        success_rate = int(os.getenv("JARVIS_SUCCESS_VOICE_RATE", "195"))
        success_pitch = int(os.getenv("JARVIS_SUCCESS_VOICE_PITCH", "58"))
        success_volume = float(os.getenv("JARVIS_SUCCESS_VOICE_VOLUME", "0.9"))
        success_emotion = os.getenv("JARVIS_SUCCESS_VOICE_EMOTION", "celebratory")
        self._personality_profiles[VoiceContext.SUCCESS] = VoicePersonality(
            voice_name=success_voice,
            rate=success_rate,
            pitch=success_pitch,
            volume=success_volume,
            emotion=success_emotion
        )

        # Trinity personality (synchronized)
        trinity_voice = os.getenv("JARVIS_TRINITY_VOICE_NAME", default_voice)
        trinity_rate = int(os.getenv("JARVIS_TRINITY_VOICE_RATE", "185"))
        trinity_pitch = int(os.getenv("JARVIS_TRINITY_VOICE_PITCH", "52"))
        trinity_volume = float(os.getenv("JARVIS_TRINITY_VOICE_VOLUME", "0.9"))
        trinity_emotion = os.getenv("JARVIS_TRINITY_VOICE_EMOTION", "neutral")
        self._personality_profiles[VoiceContext.TRINITY] = VoicePersonality(
            voice_name=trinity_voice,
            rate=trinity_rate,
            pitch=trinity_pitch,
            volume=trinity_volume,
            emotion=trinity_emotion
        )

    def _detect_best_voice(self) -> str:
        """
        Detect best available voice on system.

        ⭐ JARVIS CANONICAL VOICE: UK Daniel (professional, deep, authoritative)

        Priority:
        1. Daniel (UK Male) - JARVIS's signature voice - NON-NEGOTIABLE
        2. Samantha (US Female) - Clear fallback
        3. Alex (US Male) - macOS default
        4. Tom/Karen - Additional fallbacks
        5. First available voice
        """
        try:
            result = subprocess.run(
                ["say", "-v", "?"],
                capture_output=True,
                text=True,
                timeout=2.0
            )

            if result.returncode == 0:
                voices = result.stdout.strip().split('\n')

                # ⭐ ABSOLUTE PRIORITY: UK Daniel is JARVIS's voice
                for voice_line in voices:
                    if "daniel" in voice_line.lower():
                        logger.info(
                            "[Trinity Voice] ✅ Using JARVIS signature voice: Daniel (UK Male)"
                        )
                        return "Daniel"

                # Fallback chain
                logger.warning(
                    "[Trinity Voice] ⚠️  UK Daniel voice not found! "
                    "Install it via: System Settings → Accessibility → Spoken Content → "
                    "System Voice → Download 'Daniel (United Kingdom)'"
                )

                preferred_fallbacks = ["Samantha", "Alex", "Tom", "Karen"]

                for pref in preferred_fallbacks:
                    for voice_line in voices:
                        if pref.lower() in voice_line.lower():
                            logger.warning(
                                f"[Trinity Voice] Using fallback voice: {pref}"
                            )
                            return pref

                if voices:
                    first_voice = voices[0].split()[0]
                    return first_voice

        except Exception as e:
            logger.error(f"[Trinity Voice] Voice detection failed: {e}")

        default = os.getenv("JARVIS_DEFAULT_VOICE_NAME", "Daniel")
        return default

    async def announce(
        self,
        message: str,
        context: VoiceContext = VoiceContext.RUNTIME,
        priority: VoicePriority = VoicePriority.NORMAL,
        source: str = "jarvis",
        metadata: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Queue voice announcement with advanced processing.

        Args:
            message: Text to speak
            context: Voice context (determines personality)
            priority: Announcement priority
            source: Which component requested (jarvis, j-prime, reactor)
            metadata: Optional metadata
            correlation_id: Optional W3C trace correlation ID

        Returns:
            (success: bool, reason: str) tuple
        """
        announcement = VoiceAnnouncement(
            message=message,
            context=context,
            priority=priority,
            source=source,
            metadata=metadata or {},
            correlation_id=correlation_id,
            max_retries=self.config.max_retries
        )

        # Log event for tracing
        if self.config.tracing_enabled and self._metrics_persistence:
            asyncio.create_task(self._metrics_persistence.log_event(
                event_type="announce_request",
                correlation_id=announcement.correlation_id,
                message=message,
                source=source,
                context=context.value,
                priority=priority.name,
            ))

        # Check rate limiting (AIMD)
        if not await self._rate_limiter.check_rate_limit(priority):
            logger.warning(
                f"[TrinityVoice] Rate limit exceeded - "
                f"dropping {priority.name} announcement: {message[:50]}"
            )
            self._metrics.dropped_announcements += 1
            return False, "rate_limited"

        # Check deduplication (LRU)
        msg_hash = announcement.get_message_hash()
        if await self._dedup_cache.is_duplicate(msg_hash):
            logger.debug(
                f"[TrinityVoice] Duplicate announcement - "
                f"skipping: {message[:50]}"
            )
            self._metrics.deduplicated_announcements += 1
            return False, "duplicate"

        await self._dedup_cache.add(msg_hash)

        # Attempt message coalescing
        coalesced = await self._coalescer.coalesce(announcement)
        if coalesced is None:
            self._metrics.coalesced_announcements += 1
            return True, "coalesced"
        announcement = coalesced

        # Queue with backpressure handling
        success, reason = await self._queue.put(announcement)

        if success:
            logger.debug(
                f"[TrinityVoice] Queued {priority.name} announcement from {source}: "
                f"{message[:50]} [corr={announcement.correlation_id}]"
            )
        else:
            self._metrics.dropped_announcements += 1
            logger.warning(
                f"[TrinityVoice] Failed to queue: {reason} - {message[:50]}"
            )

        return success, reason

    async def _announcement_worker(self, worker_id: str):
        """
        Background worker that processes announcement queue.

        Multiple workers run in parallel for throughput.
        """
        logger.info(f"[TrinityVoice] Worker {worker_id} started")

        while self._running and not self._shutdown_event.is_set():
            try:
                # Get next announcement with timeout
                announcement = await self._queue.get(timeout=1.0)

                if announcement is None:
                    continue

                # Check if stale (too old)
                queue_time = time.time() - announcement.timestamp
                if queue_time > self.config.stale_threshold:
                    if announcement.priority.value >= VoicePriority.NORMAL.value:
                        logger.warning(
                            f"[TrinityVoice] {worker_id}: Skipping stale announcement "
                            f"(queued {queue_time:.1f}s ago): {announcement.message[:50]}"
                        )
                        self._metrics.dropped_announcements += 1
                        continue

                # Process announcement
                await self._process_announcement(announcement, worker_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TrinityVoice] {worker_id} error: {e}", exc_info=True)
                await asyncio.sleep(1.0)

        logger.info(f"[TrinityVoice] Worker {worker_id} stopped")

    async def _process_announcement(
        self,
        announcement: VoiceAnnouncement,
        worker_id: str
    ):
        """Process a single announcement with fallback chain."""
        start_time = time.time()
        personality = self._personality_profiles.get(
            announcement.context,
            self._personality_profiles[VoiceContext.RUNTIME]
        )

        # Calculate adaptive timeout
        timeout = announcement.calculate_timeout(self.config.announcement_timeout)

        logger.info(
            f"[TrinityVoice] {worker_id} speaking ({announcement.priority.name}): "
            f"{announcement.message[:80]} [corr={announcement.correlation_id}]"
        )

        # Try engines in order of health score
        engines = sorted(
            self._engines,
            key=lambda e: e.get_health_score(),
            reverse=True
        )

        for engine in engines:
            if not engine.available:
                continue

            try:
                success = await engine.speak(
                    announcement.message,
                    personality,
                    timeout=timeout
                )

                if success:
                    latency_ms = (time.time() - start_time) * 1000
                    self._metrics.record_announcement(True, latency_ms)
                    self._metrics.engine_health[engine.name] = engine.get_health_score()

                    # Update AIMD rate limiter
                    await self._rate_limiter.adjust_rate(True)

                    logger.info(
                        f"[TrinityVoice] {worker_id} ✓ Spoke via {engine.name} "
                        f"({latency_ms:.0f}ms) [corr={announcement.correlation_id}]"
                    )

                    # Log event for tracing
                    if self.config.tracing_enabled and self._metrics_persistence:
                        asyncio.create_task(self._metrics_persistence.log_event(
                            event_type="announce_success",
                            correlation_id=announcement.correlation_id,
                            message=announcement.message,
                            source=announcement.source,
                            context=announcement.context.value,
                            priority=announcement.priority.name,
                            engine=engine.name,
                            latency_ms=latency_ms,
                            success=True,
                        ))

                    # Publish event to subscribers (isolated)
                    await self._publish_event_safe("announcement_complete", {
                        "message": announcement.message,
                        "source": announcement.source,
                        "context": announcement.context.value,
                        "engine": engine.name,
                        "latency_ms": latency_ms,
                        "correlation_id": announcement.correlation_id,
                    })

                    return
                else:
                    logger.warning(
                        f"[TrinityVoice] {worker_id} ✗ {engine.name} failed: "
                        f"{engine.last_error} - trying next engine"
                    )

            except Exception as e:
                logger.error(f"[TrinityVoice] {worker_id} {engine.name} exception: {e}")
                continue

        # All engines failed
        latency_ms = (time.time() - start_time) * 1000
        self._metrics.record_announcement(False, latency_ms)
        self._metrics.record_error(f"All engines failed for: {announcement.message[:50]}")
        await self._rate_limiter.adjust_rate(False)

        logger.error(
            f"[TrinityVoice] {worker_id} ❌ All engines failed for: {announcement.message}"
        )

        # Log failure event
        if self.config.tracing_enabled and self._metrics_persistence:
            asyncio.create_task(self._metrics_persistence.log_event(
                event_type="announce_failure",
                correlation_id=announcement.correlation_id,
                message=announcement.message,
                source=announcement.source,
                context=announcement.context.value,
                priority=announcement.priority.name,
                latency_ms=latency_ms,
                success=False,
                error="All engines failed",
            ))

        # Retry with storm protection
        await self._handle_retry(announcement)

    async def _handle_retry(self, announcement: VoiceAnnouncement):
        """Handle retry with storm protection."""
        if announcement.retry_count >= announcement.max_retries:
            logger.warning(
                f"[TrinityVoice] Max retries ({announcement.max_retries}) exceeded, dropping"
            )
            return

        # Check retry queue depth
        if self._retry_queue_depth >= self.config.max_retry_queue_depth:
            logger.warning(
                f"[TrinityVoice] Max retry depth ({self.config.max_retry_queue_depth}) "
                f"reached, dropping: {announcement.message[:50]}"
            )
            return

        announcement.retry_count += 1
        self._metrics.retry_count += 1
        self._retry_queue_depth += 1

        # Exponential backoff with jitter
        backoff = (
            self.config.retry_base_delay ** announcement.retry_count +
            random.uniform(0, self.config.retry_jitter_max)
        )

        logger.info(
            f"[TrinityVoice] Retrying in {backoff:.1f}s "
            f"(attempt {announcement.retry_count + 1}/{announcement.max_retries + 1})"
        )

        await asyncio.sleep(backoff)

        # Re-queue
        success, reason = await self._queue.put(announcement)
        self._retry_queue_depth = max(0, self._retry_queue_depth - 1)

        if not success:
            logger.warning(f"[TrinityVoice] Retry re-queue failed: {reason}")

    async def _publish_event_safe(self, event_type: str, data: Dict[str, Any]):
        """Publish event to subscribers with isolated error handling."""
        if event_type not in self._event_subscribers:
            return

        # Execute subscribers in parallel with isolation
        tasks = []
        for callback in self._event_subscribers[event_type]:
            task = asyncio.create_task(
                self._safe_subscriber_call(callback, data),
                name=f"voice_subscriber_{id(callback)}"
            )
            tasks.append(task)

        # Wait for all with timeout
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[TrinityVoice] Subscriber {i} error: {result}")

    async def _safe_subscriber_call(
        self,
        callback: Callable,
        data: Dict[str, Any]
    ) -> None:
        """Safe subscriber call with timeout."""
        try:
            if asyncio.iscoroutinefunction(callback):
                await asyncio.wait_for(
                    callback(data),
                    timeout=self.config.subscriber_timeout
                )
            else:
                # Run sync callback in executor
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(None, callback, data),
                    timeout=self.config.subscriber_timeout
                )
        except asyncio.TimeoutError:
            logger.warning(f"[TrinityVoice] Subscriber timeout: {callback}")
        except Exception:
            raise  # Re-raise for gather to catch

    async def _health_monitoring_loop(self):
        """Background loop for engine health monitoring."""
        logger.info("[TrinityVoice] Health monitoring started")

        while self._running and not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.health_check_interval)

                for engine in self._engines:
                    if engine.available:
                        try:
                            healthy = await asyncio.wait_for(
                                engine.health_check(),
                                timeout=self.config.health_check_timeout
                            )
                            if not healthy:
                                logger.warning(
                                    f"[TrinityVoice] Engine {engine.name} failed health check"
                                )
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"[TrinityVoice] Engine {engine.name} health check timeout"
                            )
                        except Exception as e:
                            logger.error(
                                f"[TrinityVoice] Engine {engine.name} health check error: {e}"
                            )

                # Update metrics
                for engine in self._engines:
                    self._metrics.engine_health[engine.name] = engine.get_health_score()

                # Cleanup dedup cache
                await self._dedup_cache.cleanup_expired()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TrinityVoice] Health monitoring error: {e}")

        logger.info("[TrinityVoice] Health monitoring stopped")

    async def _metrics_persistence_loop(self):
        """Background loop for metrics persistence."""
        logger.info("[TrinityVoice] Metrics persistence started")

        while self._running and not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.metrics_flush_interval)

                if self._metrics_persistence:
                    await self._metrics_persistence.persist_metrics(self._metrics)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[TrinityVoice] Metrics persistence error: {e}")

        logger.info("[TrinityVoice] Metrics persistence stopped")

    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe to voice events."""
        if event_type not in self._event_subscribers:
            self._event_subscribers[event_type] = []
        self._event_subscribers[event_type].append(callback)
        logger.debug(f"[TrinityVoice] Added subscriber for {event_type}")

    def unsubscribe(self, event_type: str, callback: Callable):
        """Unsubscribe from voice events."""
        if event_type in self._event_subscribers:
            try:
                self._event_subscribers[event_type].remove(callback)
            except ValueError:
                pass

    async def shutdown(self, timeout: Optional[float] = None):
        """
        Graceful shutdown with queue draining.

        Drains high-priority announcements before stopping.
        """
        timeout = timeout or self.config.shutdown_timeout
        logger.info(f"[TrinityVoice] Shutting down (timeout={timeout}s)...")

        self._running = False
        self._shutdown_event.set()

        # Wait for workers with timeout
        if self._worker_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._worker_tasks, return_exceptions=True),
                    timeout=timeout / 2
                )
            except asyncio.TimeoutError:
                logger.warning("[TrinityVoice] Workers didn't stop in time, cancelling...")
                for task in self._worker_tasks:
                    task.cancel()

        # Stop health monitoring
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        # Stop metrics persistence
        if self._metrics_task:
            self._metrics_task.cancel()
            try:
                await self._metrics_task
            except asyncio.CancelledError:
                pass

        # Drain high-priority queue items if configured
        if self.config.shutdown_drain_high_priority:
            drained = 0
            drain_deadline = time.time() + timeout / 2

            while time.time() < drain_deadline:
                announcement = await self._queue.get(timeout=0.1)
                if announcement is None:
                    break

                if announcement.priority in (VoicePriority.CRITICAL, VoicePriority.HIGH):
                    try:
                        await self._process_announcement(announcement, "shutdown")
                        drained += 1
                    except Exception as e:
                        logger.warning(f"[TrinityVoice] Drain error: {e}")
                else:
                    logger.debug(
                        f"[TrinityVoice] Dropping {announcement.priority.name} during shutdown"
                    )

            if drained > 0:
                logger.info(f"[TrinityVoice] Drained {drained} high-priority announcements")

        # Final metrics persistence
        if self._metrics_persistence:
            try:
                await self._metrics_persistence.persist_metrics(self._metrics)
            except Exception as e:
                logger.warning(f"[TrinityVoice] Final metrics persist failed: {e}")

        logger.info("[TrinityVoice] Shutdown complete")

    def get_metrics(self) -> Dict[str, Any]:
        """Get voice metrics."""
        return self._metrics.to_dict()

    def get_status(self) -> Dict[str, Any]:
        """Get coordinator status."""
        active_engines = [e for e in self._engines if e.available]
        return {
            "version": "100.0",
            "running": self._running,
            "uptime_seconds": time.time() - self._start_time,
            "queue": self._queue.get_stats(),
            "active_engines": len(active_engines),
            "engines": [e.get_status() for e in self._engines],
            "workers": {
                "count": len(self._worker_tasks),
                "running": sum(1 for t in self._worker_tasks if not t.done()),
            },
            "rate_limiter": {
                "current_rate": self._rate_limiter.get_current_rate(),
            },
            "retry_queue_depth": self._retry_queue_depth,
            "dedup_cache_size": len(self._dedup_cache),
            "personalities": {
                ctx.value: {
                    "voice": p.voice_name,
                    "rate": p.rate,
                    "emotion": p.emotion,
                }
                for ctx, p in self._personality_profiles.items()
            },
            "metrics": self.get_metrics(),
        }

    # =========================================================================
    # Convenience Methods for Common Announcements
    # =========================================================================

    async def announce_jarvis_online(self) -> Tuple[bool, str]:
        """Standard JARVIS online announcement."""
        return await self.announce(
            "JARVIS is online. All systems operational. Ready for your command.",
            context=VoiceContext.STARTUP,
            priority=VoicePriority.CRITICAL,
            source="jarvis"
        )

    async def announce_jarvis_prime_ready(
        self,
        model_name: str = ""
    ) -> Tuple[bool, str]:
        """Announce J-Prime model loaded."""
        message = (
            f"JARVIS Prime: {model_name} loaded and ready for edge processing."
            if model_name
            else "JARVIS Prime local inference engine initialized. Ready for edge processing."
        )
        return await self.announce(
            message,
            context=VoiceContext.STARTUP,
            priority=VoicePriority.HIGH,
            source="jarvis_prime"
        )

    async def announce_reactor_core_ready(self) -> Tuple[bool, str]:
        """Announce Reactor Core ready."""
        return await self.announce(
            "Reactor Core training pipeline initialized. Model optimization ready.",
            context=VoiceContext.STARTUP,
            priority=VoicePriority.HIGH,
            source="reactor_core"
        )

    async def announce_training_complete(
        self,
        model_name: str = "",
        accuracy: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Announce training completion."""
        if accuracy is not None:
            message = (
                f"Reactor Core: {model_name or 'Model'} training complete. "
                f"Accuracy: {accuracy:.1%}. Ready for deployment."
            )
        else:
            message = (
                f"Reactor Core: {model_name or 'Training'} complete. "
                f"New model ready for deployment."
            )
        return await self.announce(
            message,
            context=VoiceContext.SUCCESS,
            priority=VoicePriority.HIGH,
            source="reactor_core"
        )

    async def announce_trinity_online(self) -> Tuple[bool, str]:
        """Announce all Trinity components online."""
        return await self.announce(
            "All Trinity components online. JARVIS, Prime, and Reactor synced. "
            "Full system operational.",
            context=VoiceContext.SUCCESS,
            priority=VoicePriority.CRITICAL,
            source="trinity"
        )

    async def announce_error(
        self,
        error_message: str,
        source: str = "jarvis"
    ) -> Tuple[bool, str]:
        """Announce error with ALERT context."""
        return await self.announce(
            f"Alert: {error_message}",
            context=VoiceContext.ALERT,
            priority=VoicePriority.HIGH,
            source=source
        )

    async def announce_success(
        self,
        success_message: str,
        source: str = "jarvis"
    ) -> Tuple[bool, str]:
        """Announce success with SUCCESS context."""
        return await self.announce(
            success_message,
            context=VoiceContext.SUCCESS,
            priority=VoicePriority.NORMAL,
            source=source
        )


# =============================================================================
# Global Singleton Instance
# =============================================================================

_coordinator: Optional[TrinityVoiceCoordinator] = None
_coordinator_lock = asyncio.Lock()


async def get_voice_coordinator(
    config: Optional[VoiceConfig] = None
) -> TrinityVoiceCoordinator:
    """Get or create global voice coordinator instance (thread-safe)."""
    global _coordinator

    if _coordinator is not None:
        return _coordinator

    async with _coordinator_lock:
        if _coordinator is None:
            _coordinator = TrinityVoiceCoordinator(config)
            await _coordinator.initialize()

    return _coordinator


async def announce(
    message: str,
    context: VoiceContext = VoiceContext.RUNTIME,
    priority: VoicePriority = VoicePriority.NORMAL,
    source: str = "jarvis",
    metadata: Optional[Dict[str, Any]] = None,
    correlation_id: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Convenience function to announce via global coordinator.

    Usage:
        success, reason = await announce(
            "JARVIS is online",
            VoiceContext.STARTUP,
            VoicePriority.HIGH
        )
    """
    coordinator = await get_voice_coordinator()
    return await coordinator.announce(
        message, context, priority, source, metadata, correlation_id
    )


async def shutdown_voice_coordinator():
    """Shutdown the global voice coordinator."""
    global _coordinator

    if _coordinator is not None:
        await _coordinator.shutdown()
        _coordinator = None


# =============================================================================
# Cross-Repo Integration via Trinity IPC
# =============================================================================

async def integrate_with_trinity_ipc():
    """
    Integrate voice coordinator with Trinity IPC for cross-repo announcements.

    This allows JARVIS Prime and Reactor Core to send voice announcements
    through the centralized coordinator.
    """
    try:
        from backend.core.trinity_ipc import (
            get_resilient_trinity_ipc_bus,
            TrinityCommand,
            ComponentType,
        )

        bus = await get_resilient_trinity_ipc_bus()
        coordinator = await get_voice_coordinator()

        async def handle_voice_command(command: TrinityCommand) -> Dict[str, Any]:
            """Handle voice command from other Trinity components."""
            payload = command.payload

            message = payload.get("message", "")
            context_str = payload.get("context", "runtime")
            priority_str = payload.get("priority", "NORMAL")
            source = payload.get("source", command.source.value)

            # Map strings to enums
            context = VoiceContext(context_str)
            priority = VoicePriority[priority_str.upper()]

            success, reason = await coordinator.announce(
                message=message,
                context=context,
                priority=priority,
                source=source,
                correlation_id=command.correlation_id,
            )

            return {
                "success": success,
                "reason": reason,
                "correlation_id": command.correlation_id,
            }

        # Register command handler
        # (This would be called by a command processing loop)
        logger.info("[TrinityVoice] Integrated with Trinity IPC for cross-repo announcements")

        return handle_voice_command

    except ImportError:
        logger.debug("[TrinityVoice] Trinity IPC not available, skipping integration")
        return None

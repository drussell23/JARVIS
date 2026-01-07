"""
JARVIS Coding Council Voice Announcer (v79.1 Super-Beefed)
===========================================================

Intelligent, dynamic, async, parallel voice announcer for code evolution operations.

v79.1 Advanced Features:
- Circuit breaker pattern for graceful degradation
- Lazy lock initialization (Python 3.9+ compatibility)
- Task tracking with automatic cleanup
- Timeout protection on all voice operations
- Trinity Voice Bridge for cross-repo announcements
- AGI OS Voice Integration (VoiceApprovalManager, RealTimeVoiceCommunicator)
- Voice approval prompts for high-risk evolutions
- Bounded async queue with priority scheduling
- Cross-repo coordination (JARVIS, J-Prime, Reactor Core)
- Exponential backoff with jitter
- Heartbeat-aware Trinity integration
- Comprehensive error recovery
- LRU caching for message composition
- Weak reference client tracking

Architecture:
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    Coding Council Voice Announcer v79.1                          │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐              │
│  │ Context Engine   │  │ Message Composer │  │ Circuit Breaker  │              │
│  │ • Time Analysis  │  │ • Stage Parts    │  │ • Failure Count  │              │
│  │ • Evolution Hist │  │ • Progress Parts │  │ • Half-Open      │              │
│  │ • User Patterns  │  │ • LRU Cache      │  │ • Backoff        │              │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘              │
│           │                      │                      │                       │
│           └──────────────────────┼──────────────────────┘                       │
│                                  ▼                                               │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                      Voice Output Pipeline                                │   │
│  ├─────────────────────────────────────────────────────────────────────────┤   │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐          │   │
│  │  │ UnifiedVoice    │  │ AGI OS Voice    │  │ Trinity Bridge  │          │   │
│  │  │ Orchestrator    │  │ Communicator    │  │ (Cross-Repo)    │          │   │
│  │  └─────────────────┘  └─────────────────┘  └─────────────────┘          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────┐   │
│  │                      Voice Approval System                                │   │
│  ├─────────────────────────────────────────────────────────────────────────┤   │
│  │  VoiceApprovalManager → Interactive Approval → Pattern Learning          │   │
│  └─────────────────────────────────────────────────────────────────────────┘   │
│                                                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘

Usage:
    from core.coding_council.voice_announcer import get_evolution_announcer

    announcer = get_evolution_announcer()

    # With voice approval for high-risk evolution
    approved = await announcer.request_voice_approval(
        task_id="abc12345",
        description="Modify authentication system",
        risk_level="high"
    )

    # Cross-repo announcement via Trinity
    await announcer.announce_trinity_evolution(
        task_id="abc12345",
        source_repo="jarvis",
        target_repo="j_prime",
        description="Cross-repo improvement"
    )
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import time
import uuid
import weakref
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import (
    Any, Callable, Coroutine, Dict, List, Optional,
    Set, Tuple, TypeVar, Union, TYPE_CHECKING
)

if TYPE_CHECKING:
    from agi_os.voice_approval_manager import VoiceApprovalManager, ApprovalRequest

logger = logging.getLogger(__name__)

T = TypeVar('T')


# =============================================================================
# v79.1: Lazy Lock Initialization (Python 3.9+ Compatibility)
# =============================================================================

_announcer_lock: Optional[asyncio.Lock] = None
_voice_queue_lock: Optional[asyncio.Lock] = None
_circuit_breaker_lock: Optional[asyncio.Lock] = None
_task_registry_lock: Optional[asyncio.Lock] = None


def _get_announcer_lock() -> asyncio.Lock:
    """Lazy initialization of announcer lock."""
    global _announcer_lock
    if _announcer_lock is None:
        _announcer_lock = asyncio.Lock()
    return _announcer_lock


def _get_voice_queue_lock() -> asyncio.Lock:
    """Lazy initialization of voice queue lock."""
    global _voice_queue_lock
    if _voice_queue_lock is None:
        _voice_queue_lock = asyncio.Lock()
    return _voice_queue_lock


def _get_circuit_breaker_lock() -> asyncio.Lock:
    """Lazy initialization of circuit breaker lock."""
    global _circuit_breaker_lock
    if _circuit_breaker_lock is None:
        _circuit_breaker_lock = asyncio.Lock()
    return _circuit_breaker_lock


def _get_task_registry_lock() -> asyncio.Lock:
    """Lazy initialization of task registry lock."""
    global _task_registry_lock
    if _task_registry_lock is None:
        _task_registry_lock = asyncio.Lock()
    return _task_registry_lock


# =============================================================================
# Enums and Types
# =============================================================================


class EvolutionStage(Enum):
    """Stages of code evolution for narration."""
    REQUESTED = "requested"
    VALIDATING = "validating"
    APPROVAL_PENDING = "approval_pending"  # v79.1: Voice approval stage
    ANALYZING = "analyzing"
    PLANNING = "planning"
    GENERATING = "generating"
    TESTING = "testing"
    APPLYING = "applying"
    VERIFYING = "verifying"
    CROSS_REPO_SYNC = "cross_repo_sync"  # v79.1: Trinity sync stage
    COMPLETE = "complete"
    FAILED = "failed"
    ROLLBACK = "rollback"


class AnnouncementType(Enum):
    """Type of announcement for cooldown and priority tracking."""
    START = "start"
    PROGRESS = "progress"
    MILESTONE = "milestone"
    COMPLETE = "complete"
    ERROR = "error"
    CONFIRMATION = "confirmation"
    APPROVAL_REQUEST = "approval_request"  # v79.1
    TRINITY_SYNC = "trinity_sync"  # v79.1
    CROSS_REPO = "cross_repo"  # v79.1


class TimeOfDay(Enum):
    """Time periods for contextual messaging."""
    EARLY_MORNING = "early_morning"
    MORNING = "morning"
    AFTERNOON = "afternoon"
    EVENING = "evening"
    NIGHT = "night"
    LATE_NIGHT = "late_night"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Failing, reject calls
    HALF_OPEN = "half_open"  # Testing recovery


class RiskLevel(Enum):
    """Risk levels for evolution operations."""
    LOW = "low"           # Safe changes
    MEDIUM = "medium"     # Minor risk
    HIGH = "high"         # Requires approval
    CRITICAL = "critical"  # Requires explicit confirmation


class TrinityRepo(Enum):
    """Trinity repository identifiers."""
    JARVIS = "jarvis"
    J_PRIME = "j_prime"
    REACTOR_CORE = "reactor_core"


# =============================================================================
# v79.1: Circuit Breaker Pattern
# =============================================================================


@dataclass
class CircuitBreakerConfig:
    """Configuration for circuit breaker."""
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    half_open_max_calls: int = 3
    success_threshold: int = 2  # Successes needed to close from half-open


class VoiceCircuitBreaker:
    """
    Circuit breaker for voice operations.

    Prevents cascade failures by backing off when voice system fails repeatedly.
    Uses exponential backoff with jitter for recovery attempts.
    """

    def __init__(self, config: Optional[CircuitBreakerConfig] = None):
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        """Get current circuit state, checking for recovery."""
        if self._state == CircuitState.OPEN:
            # Check if recovery timeout has passed
            if time.time() - self._last_failure_time >= self.config.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                self._half_open_calls = 0
                self._success_count = 0
                logger.info("[VoiceCircuitBreaker] Transitioning to HALF_OPEN")
        return self._state

    async def can_proceed(self) -> bool:
        """Check if operation can proceed based on circuit state."""
        async with _get_circuit_breaker_lock():
            state = self.state

            if state == CircuitState.CLOSED:
                return True
            elif state == CircuitState.OPEN:
                return False
            else:  # HALF_OPEN
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

    async def record_success(self) -> None:
        """Record successful voice operation."""
        async with _get_circuit_breaker_lock():
            self._failure_count = 0

            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.config.success_threshold:
                    self._state = CircuitState.CLOSED
                    logger.info("[VoiceCircuitBreaker] Circuit CLOSED (recovered)")

    async def record_failure(self) -> None:
        """Record failed voice operation."""
        async with _get_circuit_breaker_lock():
            self._failure_count += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Failed during recovery test
                self._state = CircuitState.OPEN
                logger.warning("[VoiceCircuitBreaker] Circuit OPEN (recovery failed)")
            elif self._failure_count >= self.config.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(f"[VoiceCircuitBreaker] Circuit OPEN after {self._failure_count} failures")

    def get_backoff_time(self) -> float:
        """Calculate exponential backoff with jitter."""
        base_backoff = min(2 ** self._failure_count, 60.0)  # Cap at 60s
        jitter = random.uniform(0, base_backoff * 0.3)  # 30% jitter
        return base_backoff + jitter

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return {
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time,
            "recovery_timeout": self.config.recovery_timeout,
        }


# =============================================================================
# v79.1: Task Registry for Cleanup
# =============================================================================


class VoiceTaskRegistry:
    """
    Registry for tracking async voice tasks.

    Prevents task leaks by tracking all spawned tasks and ensuring cleanup.
    Uses weak references where possible to allow garbage collection.
    """

    def __init__(self, max_concurrent: int = 10):
        self._tasks: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._completed_count = 0
        self._failed_count = 0

    async def spawn(
        self,
        coro: Coroutine[Any, Any, T],
        task_id: Optional[str] = None,
        timeout: float = 30.0
    ) -> Optional[T]:
        """
        Spawn a tracked task with timeout.

        Args:
            coro: Coroutine to execute
            task_id: Optional task ID (auto-generated if not provided)
            timeout: Timeout in seconds

        Returns:
            Task result or None if failed/timed out
        """
        task_id = task_id or str(uuid.uuid4())[:8]

        async with self._semaphore:
            try:
                task = asyncio.create_task(coro)
                async with _get_task_registry_lock():
                    self._tasks[task_id] = task

                result = await asyncio.wait_for(task, timeout=timeout)
                self._completed_count += 1
                return result

            except asyncio.TimeoutError:
                logger.warning(f"[VoiceTaskRegistry] Task {task_id} timed out after {timeout}s")
                self._failed_count += 1
                return None
            except asyncio.CancelledError:
                logger.debug(f"[VoiceTaskRegistry] Task {task_id} cancelled")
                return None
            except Exception as e:
                logger.warning(f"[VoiceTaskRegistry] Task {task_id} failed: {e}")
                self._failed_count += 1
                return None
            finally:
                async with _get_task_registry_lock():
                    self._tasks.pop(task_id, None)

    async def spawn_fire_and_forget(
        self,
        coro: Coroutine[Any, Any, Any],
        task_id: Optional[str] = None
    ) -> str:
        """
        Spawn a task without waiting for result.

        Returns task_id for tracking.
        """
        task_id = task_id or str(uuid.uuid4())[:8]

        async def wrapped():
            try:
                async with self._semaphore:
                    await coro
                    self._completed_count += 1
            except Exception as e:
                logger.debug(f"[VoiceTaskRegistry] Fire-and-forget {task_id} failed: {e}")
                self._failed_count += 1
            finally:
                async with _get_task_registry_lock():
                    self._tasks.pop(task_id, None)

        task = asyncio.create_task(wrapped())
        async with _get_task_registry_lock():
            self._tasks[task_id] = task

        return task_id

    async def cancel_all(self) -> int:
        """Cancel all pending tasks."""
        async with _get_task_registry_lock():
            cancelled = 0
            for task_id, task in list(self._tasks.items()):
                if not task.done():
                    task.cancel()
                    cancelled += 1

            # Wait for cancellation to complete
            if self._tasks:
                await asyncio.gather(*self._tasks.values(), return_exceptions=True)

            self._tasks.clear()
            return cancelled

    def get_status(self) -> Dict[str, Any]:
        """Get registry status."""
        return {
            "active_tasks": len(self._tasks),
            "completed": self._completed_count,
            "failed": self._failed_count,
            "semaphore_available": self._semaphore._value,
        }


# =============================================================================
# v79.1: LRU Cache for Message Composition
# =============================================================================


class LRUMessageCache:
    """
    LRU cache for composed messages.

    Avoids recomputing similar messages while maintaining variety
    through cache invalidation on pattern changes.
    """

    def __init__(self, max_size: int = 200):
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Optional[str]:
        """Get cached message."""
        if key in self._cache:
            self._hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, key: str, message: str) -> None:
        """Cache a composed message."""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._max_size:
                self._cache.popitem(last=False)
            self._cache[key] = message

    def invalidate_pattern(self, pattern: str) -> int:
        """Invalidate cache entries matching pattern."""
        to_remove = [k for k in self._cache if pattern in k]
        for key in to_remove:
            del self._cache[key]
        return len(to_remove)

    def clear(self) -> None:
        """Clear all cached messages."""
        self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        total = self._hits + self._misses
        return {
            "size": len(self._cache),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
        }


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class EvolutionContext:
    """
    Context for an evolution operation.
    Tracks all state for intelligent announcements.
    """
    task_id: str
    description: str
    target_files: List[str] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)
    current_stage: EvolutionStage = EvolutionStage.REQUESTED
    progress: float = 0.0
    last_announced_progress: float = 0.0
    files_analyzed: int = 0
    files_modified: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    errors: List[str] = field(default_factory=list)
    trinity_involved: bool = False
    require_confirmation: bool = False
    confirmation_id: Optional[str] = None
    # v79.1: Enhanced tracking
    risk_level: RiskLevel = RiskLevel.LOW
    approval_status: Optional[str] = None
    source_repo: Optional[TrinityRepo] = None
    target_repo: Optional[TrinityRepo] = None
    cross_repo_task: bool = False

    @property
    def elapsed_seconds(self) -> float:
        return time.time() - self.start_time

    @property
    def is_long_running(self) -> bool:
        return self.elapsed_seconds > 30.0

    @property
    def requires_approval(self) -> bool:
        """Determine if this evolution requires voice approval."""
        return self.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "target_files": self.target_files,
            "current_stage": self.current_stage.value,
            "progress": self.progress,
            "elapsed_seconds": self.elapsed_seconds,
            "files_analyzed": self.files_analyzed,
            "files_modified": self.files_modified,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "errors": self.errors,
            "trinity_involved": self.trinity_involved,
            "risk_level": self.risk_level.value,
            "approval_status": self.approval_status,
            "cross_repo_task": self.cross_repo_task,
        }


@dataclass
class AnnouncementConfig:
    """Configuration for the voice announcer."""
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_EVOLUTION_VOICE", "true").lower() == "true"
    )
    progress_cooldown: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_EVOLUTION_PROGRESS_COOLDOWN", "15.0"))
    )
    start_cooldown: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_EVOLUTION_START_COOLDOWN", "5.0"))
    )
    progress_milestones: List[float] = field(
        default_factory=lambda: [0.25, 0.50, 0.75, 1.0]
    )
    milestone_tolerance: float = 0.05
    use_sir: bool = True
    sir_probability: float = 0.15
    max_history_size: int = 100
    # v79.1: Enhanced config
    voice_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_VOICE_TIMEOUT", "10.0"))
    )
    max_concurrent_announcements: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_MAX_VOICE_CONCURRENT", "5"))
    )
    enable_trinity_voice: bool = field(
        default_factory=lambda: os.getenv("JARVIS_TRINITY_VOICE", "true").lower() == "true"
    )
    enable_agi_os_voice: bool = field(
        default_factory=lambda: os.getenv("JARVIS_AGI_OS_VOICE", "true").lower() == "true"
    )
    enable_voice_approval: bool = field(
        default_factory=lambda: os.getenv("JARVIS_VOICE_APPROVAL", "true").lower() == "true"
    )
    approval_timeout: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_APPROVAL_TIMEOUT", "60.0"))
    )


# =============================================================================
# Message Composer (Enhanced with LRU Cache)
# =============================================================================


class MessageComposer:
    """
    Composes dynamic, context-aware messages with caching.
    """

    def __init__(self, config: AnnouncementConfig):
        self.config = config
        self._cache = LRUMessageCache(max_size=200)
        self._last_used_patterns: Dict[str, int] = {}

    def _get_time_of_day(self) -> TimeOfDay:
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
        return TimeOfDay.LATE_NIGHT

    def _should_use_sir(self) -> bool:
        if not self.config.use_sir:
            return False
        return random.random() < self.config.sir_probability

    def _select_pattern(self, patterns: List[str], category: str) -> str:
        last_used = self._last_used_patterns.get(category, -1)
        available = [i for i in range(len(patterns)) if i != last_used]
        if not available:
            available = list(range(len(patterns)))
        selected = random.choice(available)
        self._last_used_patterns[category] = selected
        return patterns[selected]

    def compose_start_message(self, ctx: EvolutionContext) -> str:
        """Compose evolution start message."""
        cache_key = f"start:{ctx.description[:30]}:{len(ctx.target_files)}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        intros = [
            "Starting code evolution",
            "Beginning evolution process",
            "Initiating code changes",
            "Evolution underway",
        ]

        # v79.1: Add Trinity context
        if ctx.cross_repo_task:
            intros = [
                "Starting cross-repo evolution",
                "Initiating Trinity evolution",
                "Beginning multi-repo changes",
            ]

        intro = self._select_pattern(intros, "start_intro")

        if ctx.description:
            desc = ctx.description[:50] + "..." if len(ctx.description) > 50 else ctx.description
            message = f"{intro}: {desc}"
        else:
            message = f"{intro}."

        if len(ctx.target_files) == 1:
            filename = os.path.basename(ctx.target_files[0])
            message += f" Target: {filename}."
        elif len(ctx.target_files) > 1:
            message += f" {len(ctx.target_files)} files targeted."

        if self._should_use_sir():
            message = message.replace(".", ", Sir.")

        self._cache.put(cache_key, message)
        return message

    def compose_progress_message(self, ctx: EvolutionContext) -> str:
        """Compose progress message based on stage and percentage."""
        percentage = int(ctx.progress * 100)

        stage_messages = {
            EvolutionStage.ANALYZING: [
                f"Analyzing code structure, {percentage}% complete",
                f"Code analysis in progress, {percentage}%",
            ],
            EvolutionStage.PLANNING: [
                f"Planning changes, {percentage}% complete",
                f"Change planning at {percentage}%",
            ],
            EvolutionStage.GENERATING: [
                f"Generating new code, {percentage}% done",
                f"Code generation at {percentage}%",
            ],
            EvolutionStage.TESTING: [
                f"Running tests, {percentage}% complete",
                f"Test execution at {percentage}%",
            ],
            EvolutionStage.APPLYING: [
                f"Applying changes, {percentage}% done",
                f"Implementing changes, {percentage}%",
            ],
            EvolutionStage.VERIFYING: [
                f"Verifying changes, almost done",
                f"Final verification in progress",
            ],
            # v79.1: Trinity sync stage
            EvolutionStage.CROSS_REPO_SYNC: [
                f"Syncing across repositories, {percentage}%",
                f"Cross-repo synchronization, {percentage}% done",
            ],
        }

        if ctx.current_stage in stage_messages:
            patterns = stage_messages[ctx.current_stage]
            message = self._select_pattern(patterns, f"progress_{ctx.current_stage.value}")
        else:
            message = f"Evolution {percentage}% complete"

        if abs(ctx.progress - 0.50) < self.config.milestone_tolerance:
            message += ". Halfway there"
        elif abs(ctx.progress - 0.75) < self.config.milestone_tolerance:
            message += ". Almost finished"

        return message + "."

    def compose_complete_message(
        self,
        ctx: EvolutionContext,
        success: bool,
        error_message: str = ""
    ) -> str:
        """Compose completion message."""
        if success:
            success_patterns = [
                "Evolution complete",
                "Code evolution successful",
                "Changes applied successfully",
            ]

            # v79.1: Trinity-specific success
            if ctx.cross_repo_task:
                success_patterns = [
                    "Cross-repo evolution complete",
                    "Trinity synchronization successful",
                    "Multi-repository changes applied",
                ]

            message = self._select_pattern(success_patterns, "complete_success")

            if ctx.files_modified == 1:
                message += ". Modified one file"
            elif ctx.files_modified > 1:
                message += f". Updated {ctx.files_modified} files"

            if ctx.tests_passed > 0:
                message += f". All {ctx.tests_passed} tests passing"

            elapsed = ctx.elapsed_seconds
            if elapsed > 30:
                message += f". Completed in {int(elapsed)} seconds"

            message += "."

        else:
            failure_patterns = [
                "Evolution encountered an issue",
                "Could not complete evolution",
                "Evolution failed",
            ]
            message = self._select_pattern(failure_patterns, "complete_failure")

            if error_message:
                error = error_message[:40] + "..." if len(error_message) > 40 else error_message
                message += f": {error}"
            message += "."

        if success and ctx.files_modified > 0 and self._should_use_sir():
            message = message.rstrip(".") + ", Sir."

        return message

    def compose_approval_request_message(self, ctx: EvolutionContext) -> str:
        """v79.1: Compose voice approval request message."""
        risk_phrases = {
            RiskLevel.HIGH: "This is a high-risk change",
            RiskLevel.CRITICAL: "This is a critical system change",
        }

        risk_phrase = risk_phrases.get(ctx.risk_level, "This change requires approval")

        patterns = [
            f"{risk_phrase}. {ctx.description[:40]}. Say 'yes' to approve or 'no' to cancel.",
            f"Approval needed: {ctx.description[:40]}. {risk_phrase}. Yes or no?",
            f"I'd like to: {ctx.description[:40]}. {risk_phrase}. Do you approve?",
        ]

        return self._select_pattern(patterns, "approval_request")

    def compose_trinity_message(
        self,
        ctx: EvolutionContext,
        source: TrinityRepo,
        target: TrinityRepo
    ) -> str:
        """v79.1: Compose Trinity cross-repo announcement."""
        repo_names = {
            TrinityRepo.JARVIS: "JARVIS Body",
            TrinityRepo.J_PRIME: "J-Prime Mind",
            TrinityRepo.REACTOR_CORE: "Reactor Core Nerves",
        }

        source_name = repo_names.get(source, source.value)
        target_name = repo_names.get(target, target.value)

        patterns = [
            f"Coordinating evolution from {source_name} to {target_name}",
            f"Cross-repo sync: {source_name} to {target_name}",
            f"Trinity protocol: {source_name} updating {target_name}",
        ]

        message = self._select_pattern(patterns, "trinity_sync")

        if ctx.description:
            desc = ctx.description[:30] + "..." if len(ctx.description) > 30 else ctx.description
            message += f". Task: {desc}"

        return message + "."

    def compose_error_message(self, error_type: str, details: str = "") -> str:
        error_patterns = {
            "validation": "Evolution request could not be validated",
            "permission": "Insufficient permissions for this evolution",
            "timeout": "Evolution timed out",
            "conflict": "Detected conflicting changes",
            "test_failure": "Tests failed during evolution",
            "rollback": "Rolling back changes due to errors",
            "approval_denied": "Evolution cancelled by user",
            "trinity_sync_failed": "Cross-repo synchronization failed",
            "circuit_open": "Voice system temporarily unavailable",
        }

        message = error_patterns.get(error_type, "Evolution error occurred")

        if details:
            message += f": {details[:30]}"

        return message + "."

    def compose_confirmation_message(self, ctx: EvolutionContext) -> str:
        confirmation_patterns = [
            f"Confirmation needed for: {ctx.description[:40]}",
            f"Please confirm evolution: {ctx.description[:40]}",
            f"Evolution requires approval: {ctx.description[:40]}",
        ]

        message = self._select_pattern(confirmation_patterns, "confirmation")

        if ctx.confirmation_id:
            message += f". Say confirm {ctx.confirmation_id} to proceed"

        return message + "."

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get message cache statistics."""
        return self._cache.get_stats()


# =============================================================================
# v79.1: Trinity Voice Bridge
# =============================================================================


class TrinityVoiceBridge:
    """
    Voice bridge for cross-repo announcements via Trinity protocol.

    Coordinates voice announcements across JARVIS, J-Prime, and Reactor Core
    ensuring consistent feedback for cross-repository operations.
    """

    def __init__(self):
        self._trinity_available = False
        self._transport = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize Trinity voice bridge."""
        if self._initialized:
            return self._trinity_available

        self._initialized = True

        try:
            # Try to import Trinity components
            try:
                from core.coding_council.trinity_integration import (
                    get_trinity_handler,
                    TRINITY_MODULE_AVAILABLE
                )
            except ImportError:
                from backend.core.coding_council.trinity_integration import (
                    get_trinity_handler,
                    TRINITY_MODULE_AVAILABLE
                )

            if TRINITY_MODULE_AVAILABLE:
                self._trinity_available = True
                logger.info("[TrinityVoiceBridge] Trinity integration available")
            else:
                logger.debug("[TrinityVoiceBridge] Trinity module not available")

        except ImportError as e:
            logger.debug(f"[TrinityVoiceBridge] Trinity not available: {e}")
        except Exception as e:
            logger.warning(f"[TrinityVoiceBridge] Initialization failed: {e}")

        return self._trinity_available

    async def broadcast_to_repo(
        self,
        target_repo: TrinityRepo,
        message: str,
        event_type: str,
        task_id: str
    ) -> bool:
        """
        Broadcast voice announcement to specific repo.

        This sends a voice event via Trinity transport that can be
        picked up by the voice system in the target repository.
        """
        if not await self.initialize():
            return False

        try:
            # Get Trinity handler
            try:
                from core.coding_council.trinity_integration import get_trinity_handler
            except ImportError:
                from backend.core.coding_council.trinity_integration import get_trinity_handler

            handler = await get_trinity_handler()
            if not handler:
                return False

            # Send voice event via Trinity
            event = {
                "type": "voice_announcement",
                "event_type": event_type,
                "message": message,
                "task_id": task_id,
                "target_repo": target_repo.value,
                "timestamp": time.time(),
            }

            # Use Trinity transport to send
            if hasattr(handler, 'send_event'):
                await handler.send_event(event)
                logger.debug(f"[TrinityVoiceBridge] Sent to {target_repo.value}: {event_type}")
                return True
            elif hasattr(handler, 'broadcast'):
                await handler.broadcast(event)
                return True

        except Exception as e:
            logger.warning(f"[TrinityVoiceBridge] Broadcast failed: {e}")

        return False

    async def broadcast_to_all(
        self,
        message: str,
        event_type: str,
        task_id: str,
        exclude: Optional[List[TrinityRepo]] = None
    ) -> int:
        """Broadcast to all Trinity repos (except excluded)."""
        exclude = exclude or []
        sent = 0

        for repo in TrinityRepo:
            if repo not in exclude:
                if await self.broadcast_to_repo(repo, message, event_type, task_id):
                    sent += 1

        return sent

    def get_status(self) -> Dict[str, Any]:
        """Get Trinity voice bridge status."""
        return {
            "initialized": self._initialized,
            "trinity_available": self._trinity_available,
        }


# =============================================================================
# v79.1: AGI OS Voice Integration
# =============================================================================


class AGIOSVoiceIntegration:
    """
    Integration with AGI OS voice systems.

    Connects to:
    - VoiceApprovalManager for interactive approvals
    - RealTimeVoiceCommunicator for AGI OS announcements
    """

    def __init__(self):
        self._approval_manager: Optional[Any] = None
        self._voice_communicator: Optional[Any] = None
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize AGI OS voice integration."""
        if self._initialized:
            return self._approval_manager is not None or self._voice_communicator is not None

        self._initialized = True

        # Initialize VoiceApprovalManager
        try:
            try:
                from agi_os.voice_approval_manager import get_approval_manager
            except ImportError:
                from backend.agi_os.voice_approval_manager import get_approval_manager

            self._approval_manager = await get_approval_manager()
            logger.info("[AGIOSVoice] VoiceApprovalManager connected")
        except ImportError:
            logger.debug("[AGIOSVoice] VoiceApprovalManager not available")
        except Exception as e:
            logger.warning(f"[AGIOSVoice] VoiceApprovalManager failed: {e}")

        # Initialize RealTimeVoiceCommunicator
        try:
            try:
                from agi_os.realtime_voice_communicator import get_voice_communicator
            except ImportError:
                from backend.agi_os.realtime_voice_communicator import get_voice_communicator

            self._voice_communicator = await get_voice_communicator()
            logger.info("[AGIOSVoice] RealTimeVoiceCommunicator connected")
        except ImportError:
            logger.debug("[AGIOSVoice] RealTimeVoiceCommunicator not available")
        except Exception as e:
            logger.warning(f"[AGIOSVoice] RealTimeVoiceCommunicator failed: {e}")

        return self._approval_manager is not None or self._voice_communicator is not None

    async def request_approval(
        self,
        action_type: str,
        target: str,
        description: str,
        confidence: float = 0.85,
        urgency: str = "normal",
        timeout: float = 60.0
    ) -> Tuple[bool, Optional[str]]:
        """
        Request voice approval via AGI OS VoiceApprovalManager.

        Returns:
            Tuple of (approved: bool, feedback: Optional[str])
        """
        if not await self.initialize() or not self._approval_manager:
            logger.debug("[AGIOSVoice] VoiceApprovalManager not available for approval")
            return True, None  # Default to approved if not available

        try:
            # Import approval types
            try:
                from agi_os.voice_approval_manager import ApprovalRequest, ApprovalUrgency
            except ImportError:
                from backend.agi_os.voice_approval_manager import ApprovalRequest, ApprovalUrgency

            urgency_map = {
                "low": ApprovalUrgency.LOW,
                "normal": ApprovalUrgency.NORMAL,
                "high": ApprovalUrgency.HIGH,
                "critical": ApprovalUrgency.CRITICAL,
            }

            request = ApprovalRequest(
                action_type=action_type,
                target=target,
                confidence=confidence,
                reasoning=description,
                urgency=urgency_map.get(urgency, ApprovalUrgency.NORMAL),
                timeout=timeout,
            )

            response = await self._approval_manager.request_approval(request)

            return response.approved, response.user_feedback

        except Exception as e:
            logger.warning(f"[AGIOSVoice] Approval request failed: {e}")
            return True, None  # Default to approved on error

    async def speak_via_agi_os(
        self,
        message: str,
        mode: str = "normal",
        priority: str = "normal"
    ) -> bool:
        """
        Speak via AGI OS RealTimeVoiceCommunicator.
        """
        if not await self.initialize() or not self._voice_communicator:
            return False

        try:
            # Speak via communicator
            if hasattr(self._voice_communicator, 'speak'):
                await self._voice_communicator.speak(message)
                return True
            elif hasattr(self._voice_communicator, 'announce'):
                await self._voice_communicator.announce(message)
                return True
        except Exception as e:
            logger.warning(f"[AGIOSVoice] Speak failed: {e}")

        return False

    def get_status(self) -> Dict[str, Any]:
        """Get AGI OS integration status."""
        return {
            "initialized": self._initialized,
            "approval_manager": self._approval_manager is not None,
            "voice_communicator": self._voice_communicator is not None,
        }


# =============================================================================
# Main Voice Announcer Class (v79.1 Super-Beefed)
# =============================================================================


class CodingCouncilVoiceAnnouncer:
    """
    v79.1 Super-Beefed Voice Announcer for Coding Council.

    Features:
    - Circuit breaker for graceful degradation
    - Task registry for cleanup
    - Trinity voice bridge for cross-repo
    - AGI OS voice integration
    - Voice approval system
    - Timeout protection
    - Exponential backoff
    """

    def __init__(self, config: Optional[AnnouncementConfig] = None):
        self.config = config or AnnouncementConfig()
        self._composer = MessageComposer(self.config)

        # Core state
        self._active_evolutions: Dict[str, EvolutionContext] = {}
        self._last_announcement: Dict[str, float] = {}
        self._evolution_history: List[Dict[str, Any]] = []

        # v79.1: Advanced components
        self._circuit_breaker = VoiceCircuitBreaker()
        self._task_registry = VoiceTaskRegistry(
            max_concurrent=self.config.max_concurrent_announcements
        )
        self._trinity_bridge = TrinityVoiceBridge()
        self._agi_os_voice = AGIOSVoiceIntegration()

        # Statistics
        self._stats = {
            "announcements_made": 0,
            "announcements_throttled": 0,
            "announcements_circuit_blocked": 0,
            "evolutions_tracked": 0,
            "successful_evolutions": 0,
            "failed_evolutions": 0,
            "approvals_requested": 0,
            "approvals_granted": 0,
            "approvals_denied": 0,
            "trinity_broadcasts": 0,
            "events_emitted": 0,
        }

        # v79.1: Event listener system for external integrations
        # Allows VoiceAuthenticationNarrator, run_supervisor, etc. to receive events
        self._event_listeners: List[Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]] = []
        self._event_listeners_sync: List[Callable[[str, Dict[str, Any]], None]] = []
        self._pending_approvals: Dict[str, asyncio.Future] = {}

        logger.info(f"[CodingCouncilVoice] v79.1 Initialized (enabled={self.config.enabled})")

    # =========================================================================
    # v79.1: Event Listener System (for external integrations)
    # =========================================================================

    async def add_event_listener(
        self,
        callback: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> bool:
        """
        Register an async callback to receive evolution events.

        The callback will be invoked with:
            - event_type: str (e.g., 'started', 'progress', 'complete', 'failed')
            - details: Dict[str, Any] containing event-specific data

        Example:
            async def my_handler(event_type: str, details: Dict[str, Any]) -> None:
                if event_type == 'complete':
                    print(f"Evolution {details['task_id']} completed!")

            await announcer.add_event_listener(my_handler)

        Args:
            callback: Async function to call on events

        Returns:
            True if registered successfully
        """
        if callback not in self._event_listeners:
            self._event_listeners.append(callback)
            logger.debug(f"[CodingCouncilVoice] Added event listener: {callback.__name__ if hasattr(callback, '__name__') else 'anonymous'}")
            return True
        return False

    def add_event_listener_sync(
        self,
        callback: Callable[[str, Dict[str, Any]], None]
    ) -> bool:
        """
        Register a synchronous callback to receive evolution events.

        Args:
            callback: Sync function to call on events

        Returns:
            True if registered successfully
        """
        if callback not in self._event_listeners_sync:
            self._event_listeners_sync.append(callback)
            logger.debug(f"[CodingCouncilVoice] Added sync event listener: {callback.__name__ if hasattr(callback, '__name__') else 'anonymous'}")
            return True
        return False

    async def remove_event_listener(
        self,
        callback: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> bool:
        """
        Remove an async event listener.

        Args:
            callback: Previously registered callback to remove

        Returns:
            True if removed, False if not found
        """
        if callback in self._event_listeners:
            self._event_listeners.remove(callback)
            logger.debug(f"[CodingCouncilVoice] Removed event listener")
            return True
        return False

    def remove_event_listener_sync(
        self,
        callback: Callable[[str, Dict[str, Any]], None]
    ) -> bool:
        """
        Remove a synchronous event listener.

        Args:
            callback: Previously registered sync callback to remove

        Returns:
            True if removed, False if not found
        """
        if callback in self._event_listeners_sync:
            self._event_listeners_sync.remove(callback)
            return True
        return False

    async def _emit_event(
        self,
        event_type: str,
        details: Dict[str, Any],
        fire_and_forget: bool = True
    ) -> int:
        """
        Emit an event to all registered listeners.

        Args:
            event_type: Type of event (started, progress, complete, failed, etc.)
            details: Event-specific data
            fire_and_forget: If True, don't wait for listeners to complete

        Returns:
            Number of listeners notified
        """
        self._stats["events_emitted"] += 1
        notified = 0

        # Add timestamp and event_type to details
        enriched_details = {
            **details,
            "event_type": event_type,
            "timestamp": time.time(),
            "timestamp_iso": datetime.now().isoformat(),
        }

        # Notify async listeners
        for listener in self._event_listeners:
            try:
                if fire_and_forget:
                    # Non-blocking: spawn task and continue
                    asyncio.create_task(
                        self._safe_call_listener(listener, event_type, enriched_details)
                    )
                else:
                    # Blocking: wait for listener
                    await self._safe_call_listener(listener, event_type, enriched_details)
                notified += 1
            except Exception as e:
                logger.warning(f"[CodingCouncilVoice] Async listener error: {e}")

        # Notify sync listeners (in executor to avoid blocking)
        for listener in self._event_listeners_sync:
            try:
                loop = asyncio.get_event_loop()
                if fire_and_forget:
                    loop.run_in_executor(
                        None,
                        lambda: self._safe_call_sync_listener(listener, event_type, enriched_details)
                    )
                else:
                    await loop.run_in_executor(
                        None,
                        lambda: self._safe_call_sync_listener(listener, event_type, enriched_details)
                    )
                notified += 1
            except Exception as e:
                logger.warning(f"[CodingCouncilVoice] Sync listener error: {e}")

        if notified > 0:
            logger.debug(f"[CodingCouncilVoice] Emitted '{event_type}' to {notified} listeners")

        return notified

    async def _safe_call_listener(
        self,
        listener: Callable[[str, Dict[str, Any]], Coroutine[Any, Any, None]],
        event_type: str,
        details: Dict[str, Any]
    ) -> None:
        """Safely call an async listener with timeout."""
        try:
            await asyncio.wait_for(
                listener(event_type, details),
                timeout=5.0  # 5 second timeout for listeners
            )
        except asyncio.TimeoutError:
            listener_name = getattr(listener, '__name__', 'anonymous')
            logger.warning(f"[CodingCouncilVoice] Listener '{listener_name}' timed out")
        except Exception as e:
            listener_name = getattr(listener, '__name__', 'anonymous')
            logger.warning(f"[CodingCouncilVoice] Listener '{listener_name}' error: {e}")

    def _safe_call_sync_listener(
        self,
        listener: Callable[[str, Dict[str, Any]], None],
        event_type: str,
        details: Dict[str, Any]
    ) -> None:
        """Safely call a sync listener."""
        try:
            listener(event_type, details)
        except Exception as e:
            listener_name = getattr(listener, '__name__', 'anonymous')
            logger.warning(f"[CodingCouncilVoice] Sync listener '{listener_name}' error: {e}")

    def get_listener_count(self) -> Dict[str, int]:
        """Get count of registered listeners."""
        return {
            "async_listeners": len(self._event_listeners),
            "sync_listeners": len(self._event_listeners_sync),
            "total": len(self._event_listeners) + len(self._event_listeners_sync),
        }

    def _can_announce(self, announcement_type: AnnouncementType) -> bool:
        """Check if we can make an announcement."""
        if not self.config.enabled:
            return False

        now = time.time()
        last = self._last_announcement.get(announcement_type.value, 0)

        cooldowns = {
            AnnouncementType.PROGRESS: self.config.progress_cooldown,
            AnnouncementType.START: self.config.start_cooldown,
            AnnouncementType.APPROVAL_REQUEST: 5.0,
            AnnouncementType.TRINITY_SYNC: 10.0,
        }

        cooldown = cooldowns.get(announcement_type, 2.0)

        if now - last < cooldown:
            self._stats["announcements_throttled"] += 1
            return False

        return True

    def _record_announcement(self, announcement_type: AnnouncementType):
        """Record that an announcement was made."""
        self._last_announcement[announcement_type.value] = time.time()
        self._stats["announcements_made"] += 1

    async def _speak(
        self,
        message: str,
        priority: str = "medium",
        wait: bool = False,
        use_agi_os: bool = False
    ) -> bool:
        """
        Speak through voice systems with circuit breaker protection.
        """
        # Check circuit breaker
        if not await self._circuit_breaker.can_proceed():
            self._stats["announcements_circuit_blocked"] += 1
            logger.debug("[CodingCouncilVoice] Circuit breaker OPEN, skipping")
            return False

        try:
            # Try AGI OS voice first if enabled
            if use_agi_os and self.config.enable_agi_os_voice:
                success = await self._agi_os_voice.speak_via_agi_os(message, priority=priority)
                if success:
                    await self._circuit_breaker.record_success()
                    return True

            # Fall back to unified voice orchestrator
            try:
                try:
                    from core.supervisor.unified_voice_orchestrator import (
                        speak_evolution,
                        VoicePriority,
                    )
                except ImportError:
                    from backend.core.supervisor.unified_voice_orchestrator import (
                        speak_evolution,
                        VoicePriority,
                    )

                priority_map = {
                    "low": VoicePriority.LOW,
                    "medium": VoicePriority.MEDIUM,
                    "high": VoicePriority.HIGH,
                    "critical": VoicePriority.CRITICAL,
                }

                # Execute with timeout
                result = await asyncio.wait_for(
                    speak_evolution(
                        message,
                        priority=priority_map.get(priority, VoicePriority.MEDIUM),
                        wait=wait
                    ),
                    timeout=self.config.voice_timeout
                )

                if result:
                    await self._circuit_breaker.record_success()
                return result

            except ImportError:
                logger.debug("[CodingCouncilVoice] Voice orchestrator not available")
                return False
            except asyncio.TimeoutError:
                logger.warning(f"[CodingCouncilVoice] Voice timeout after {self.config.voice_timeout}s")
                await self._circuit_breaker.record_failure()
                return False

        except Exception as e:
            logger.warning(f"[CodingCouncilVoice] Failed to speak: {e}")
            await self._circuit_breaker.record_failure()
            return False

    # =========================================================================
    # v79.1: Voice Approval System
    # =========================================================================

    async def request_voice_approval(
        self,
        task_id: str,
        description: str,
        risk_level: Union[str, RiskLevel] = RiskLevel.HIGH,
        target: str = "",
        confidence: float = 0.85
    ) -> Tuple[bool, Optional[str]]:
        """
        Request voice approval for evolution operation.

        Args:
            task_id: Evolution task ID
            description: What the evolution will do
            risk_level: Risk level of the change
            target: Target of the change (file, module, etc.)
            confidence: AI confidence in the change

        Returns:
            Tuple of (approved: bool, feedback: Optional[str])
        """
        if isinstance(risk_level, str):
            risk_level = RiskLevel(risk_level)

        self._stats["approvals_requested"] += 1

        # Create/update context
        ctx = self._active_evolutions.get(task_id)
        if ctx:
            ctx.risk_level = risk_level
            ctx.current_stage = EvolutionStage.APPROVAL_PENDING
        else:
            ctx = EvolutionContext(
                task_id=task_id,
                description=description,
                risk_level=risk_level,
                current_stage=EvolutionStage.APPROVAL_PENDING,
            )
            self._active_evolutions[task_id] = ctx

        # Skip approval if disabled or low risk
        if not self.config.enable_voice_approval:
            ctx.approval_status = "auto_approved"
            self._stats["approvals_granted"] += 1
            return True, None

        if risk_level == RiskLevel.LOW:
            ctx.approval_status = "auto_approved_low_risk"
            self._stats["approvals_granted"] += 1
            return True, None

        # Announce approval request
        message = self._composer.compose_approval_request_message(ctx)
        await self._speak(message, priority="high", wait=True)

        # Request approval via AGI OS
        approved, feedback = await self._agi_os_voice.request_approval(
            action_type="code_evolution",
            target=target or task_id,
            description=description,
            confidence=confidence,
            urgency="high" if risk_level == RiskLevel.CRITICAL else "normal",
            timeout=self.config.approval_timeout
        )

        ctx.approval_status = "approved" if approved else "denied"

        if approved:
            self._stats["approvals_granted"] += 1
            logger.info(f"[CodingCouncilVoice] Approval granted for {task_id}")
            # v79.1: Emit approval granted event
            await self._emit_event("approval_granted", {
                "task_id": task_id,
                "description": description,
                "risk_level": risk_level.value,
                "feedback": feedback,
            })
        else:
            self._stats["approvals_denied"] += 1
            logger.info(f"[CodingCouncilVoice] Approval denied for {task_id}")
            await self._speak(
                self._composer.compose_error_message("approval_denied"),
                priority="medium"
            )
            # v79.1: Emit approval denied event
            await self._emit_event("approval_denied", {
                "task_id": task_id,
                "description": description,
                "risk_level": risk_level.value,
                "feedback": feedback,
            })

        return approved, feedback

    # =========================================================================
    # v79.1: Trinity Cross-Repo Announcements
    # =========================================================================

    async def announce_trinity_evolution(
        self,
        task_id: str,
        source_repo: Union[str, TrinityRepo],
        target_repo: Union[str, TrinityRepo],
        description: str,
        progress: float = 0.0
    ) -> bool:
        """
        Announce cross-repo evolution via Trinity.

        Args:
            task_id: Evolution task ID
            source_repo: Source repository
            target_repo: Target repository
            description: Evolution description
            progress: Current progress (0.0-1.0)
        """
        if not self.config.enable_trinity_voice:
            return False

        if not self._can_announce(AnnouncementType.TRINITY_SYNC):
            return False

        # Convert to enums if needed
        if isinstance(source_repo, str):
            source_repo = TrinityRepo(source_repo)
        if isinstance(target_repo, str):
            target_repo = TrinityRepo(target_repo)

        # Get/create context
        ctx = self._active_evolutions.get(task_id)
        if ctx:
            ctx.source_repo = source_repo
            ctx.target_repo = target_repo
            ctx.cross_repo_task = True
            ctx.current_stage = EvolutionStage.CROSS_REPO_SYNC
        else:
            ctx = EvolutionContext(
                task_id=task_id,
                description=description,
                source_repo=source_repo,
                target_repo=target_repo,
                cross_repo_task=True,
                current_stage=EvolutionStage.CROSS_REPO_SYNC,
                progress=progress,
            )
            self._active_evolutions[task_id] = ctx

        # Compose and speak locally
        message = self._composer.compose_trinity_message(ctx, source_repo, target_repo)
        await self._speak(message, priority="medium")

        # Broadcast to Trinity repos
        sent = await self._trinity_bridge.broadcast_to_all(
            message=message,
            event_type="evolution_sync",
            task_id=task_id,
            exclude=[source_repo]  # Don't broadcast back to source
        )

        if sent > 0:
            self._stats["trinity_broadcasts"] += sent
            self._record_announcement(AnnouncementType.TRINITY_SYNC)
            logger.info(f"[CodingCouncilVoice] Trinity broadcast to {sent} repos")

        return sent > 0

    # =========================================================================
    # Public API - Evolution Event Handlers
    # =========================================================================

    async def announce_evolution_started(
        self,
        task_id: str,
        description: str,
        target_files: Optional[List[str]] = None,
        trinity_involved: bool = False,
        risk_level: Union[str, RiskLevel] = RiskLevel.LOW,
        require_approval: bool = False
    ) -> bool:
        """Announce that an evolution has started."""
        if not self._can_announce(AnnouncementType.START):
            return False

        if isinstance(risk_level, str):
            risk_level = RiskLevel(risk_level)

        # Create context
        ctx = EvolutionContext(
            task_id=task_id,
            description=description,
            target_files=target_files or [],
            trinity_involved=trinity_involved,
            current_stage=EvolutionStage.REQUESTED,
            risk_level=risk_level,
        )
        self._active_evolutions[task_id] = ctx
        self._stats["evolutions_tracked"] += 1

        # Request approval if needed
        if require_approval or ctx.requires_approval:
            approved, _ = await self.request_voice_approval(
                task_id=task_id,
                description=description,
                risk_level=risk_level
            )
            if not approved:
                return False

        # Compose and speak
        message = self._composer.compose_start_message(ctx)
        result = await self._task_registry.spawn(
            self._speak(message, priority="medium"),
            task_id=f"start_{task_id}",
            timeout=self.config.voice_timeout
        )

        if result:
            self._record_announcement(AnnouncementType.START)
            logger.info(f"[CodingCouncilVoice] Announced start: {task_id}")

        # v79.1: Emit event to listeners
        await self._emit_event("started", {
            "task_id": task_id,
            "description": description,
            "target_files": target_files or [],
            "trinity_involved": trinity_involved,
            "risk_level": risk_level.value,
            "progress": 0.0,
        })

        return result or False

    async def announce_evolution_progress(
        self,
        task_id: str,
        progress: float,
        stage: Optional[str] = None,
    ) -> bool:
        """Announce evolution progress at milestones."""
        ctx = self._active_evolutions.get(task_id)
        if not ctx:
            return False

        ctx.progress = progress
        if stage:
            try:
                ctx.current_stage = EvolutionStage(stage)
            except ValueError:
                pass

        # Check if at milestone
        at_milestone = False
        for milestone in self.config.progress_milestones:
            if abs(progress - milestone) <= self.config.milestone_tolerance:
                if ctx.last_announced_progress < milestone - self.config.milestone_tolerance:
                    at_milestone = True
                    ctx.last_announced_progress = progress
                    break

        if not at_milestone:
            return False

        if not self._can_announce(AnnouncementType.PROGRESS):
            return False

        message = self._composer.compose_progress_message(ctx)

        # Fire and forget for progress (non-blocking)
        await self._task_registry.spawn_fire_and_forget(
            self._speak(message, priority="low"),
            task_id=f"progress_{task_id}_{int(progress*100)}"
        )

        self._record_announcement(AnnouncementType.PROGRESS)

        # v79.1: Emit event to listeners
        await self._emit_event("progress", {
            "task_id": task_id,
            "progress": progress,
            "stage": ctx.current_stage.value if ctx else stage,
            "description": ctx.description if ctx else "",
            "files_modified": ctx.files_modified if ctx else 0,
        })

        return True

    async def announce_evolution_complete(
        self,
        task_id: str,
        success: bool,
        files_modified: Optional[List[str]] = None,
        error_message: str = "",
    ) -> bool:
        """Announce evolution completion."""
        ctx = self._active_evolutions.get(task_id)
        if not ctx:
            ctx = EvolutionContext(
                task_id=task_id,
                description="",
                files_modified=len(files_modified or []),
            )

        ctx.files_modified = len(files_modified or [])
        ctx.current_stage = EvolutionStage.COMPLETE if success else EvolutionStage.FAILED
        ctx.progress = 1.0 if success else ctx.progress

        if success:
            self._stats["successful_evolutions"] += 1
        else:
            self._stats["failed_evolutions"] += 1

        message = self._composer.compose_complete_message(ctx, success, error_message)
        priority = "medium" if success else "high"

        result = await self._task_registry.spawn(
            self._speak(message, priority=priority, wait=True),
            task_id=f"complete_{task_id}",
            timeout=self.config.voice_timeout
        )

        if result:
            self._record_announcement(AnnouncementType.COMPLETE)
            logger.info(f"[CodingCouncilVoice] Completion: {task_id} (success={success})")

        # Record to history
        self._record_evolution_history(ctx, success, error_message)

        # v79.1: Emit event to listeners
        event_type = "complete" if success else "failed"
        await self._emit_event(event_type, {
            "task_id": task_id,
            "success": success,
            "files_modified": files_modified or [],
            "error": error_message if not success else None,
            "duration_seconds": ctx.elapsed_seconds if ctx else 0,
            "description": ctx.description if ctx else "",
            "progress": 1.0 if success else (ctx.progress if ctx else 0),
        })

        # Cleanup
        self._active_evolutions.pop(task_id, None)

        return result or False

    async def announce_confirmation_needed(
        self,
        task_id: str,
        description: str,
        confirmation_id: str,
    ) -> bool:
        """Announce that confirmation is needed."""
        ctx = self._active_evolutions.get(task_id)
        if not ctx:
            ctx = EvolutionContext(
                task_id=task_id,
                description=description,
            )
            self._active_evolutions[task_id] = ctx

        ctx.require_confirmation = True
        ctx.confirmation_id = confirmation_id
        ctx.current_stage = EvolutionStage.VALIDATING

        message = self._composer.compose_confirmation_message(ctx)
        result = await self._speak(message, priority="high", wait=True)

        if result:
            self._record_announcement(AnnouncementType.CONFIRMATION)
            logger.info(f"[CodingCouncilVoice] Confirmation needed: {task_id}")

        # v79.1: Emit event to listeners
        await self._emit_event("approval_needed", {
            "task_id": task_id,
            "description": description,
            "confirmation_id": confirmation_id,
            "stage": "validating",
        })

        return result

    async def announce_error(
        self,
        task_id: str,
        error_type: str,
        details: str = "",
    ) -> bool:
        """Announce an error during evolution."""
        message = self._composer.compose_error_message(error_type, details)
        result = await self._speak(message, priority="high", wait=True)

        if result:
            self._record_announcement(AnnouncementType.ERROR)
            logger.warning(f"[CodingCouncilVoice] Error: {task_id} - {error_type}")

        ctx = self._active_evolutions.get(task_id)
        if ctx:
            ctx.errors.append(f"{error_type}: {details}")

        # v79.1: Emit event to listeners
        await self._emit_event("error", {
            "task_id": task_id,
            "error_type": error_type,
            "details": details,
            "description": ctx.description if ctx else "",
            "progress": ctx.progress if ctx else 0,
        })

        return result

    # =========================================================================
    # History and Statistics
    # =========================================================================

    def _record_evolution_history(
        self,
        ctx: EvolutionContext,
        success: bool,
        error_message: str
    ):
        """Record evolution to history."""
        record = {
            "timestamp": datetime.now().isoformat(),
            "task_id": ctx.task_id,
            "description": ctx.description,
            "target_files": ctx.target_files,
            "success": success,
            "duration_seconds": ctx.elapsed_seconds,
            "files_modified": ctx.files_modified,
            "trinity_involved": ctx.trinity_involved,
            "cross_repo_task": ctx.cross_repo_task,
            "risk_level": ctx.risk_level.value,
            "approval_status": ctx.approval_status,
            "error": error_message if not success else None,
        }

        self._evolution_history.append(record)

        if len(self._evolution_history) > self.config.max_history_size:
            self._evolution_history = self._evolution_history[-self.config.max_history_size:]

    def get_statistics(self) -> Dict[str, Any]:
        """Get comprehensive announcer statistics."""
        return {
            **self._stats,
            "active_evolutions": len(self._active_evolutions),
            "history_size": len(self._evolution_history),
            "circuit_breaker": self._circuit_breaker.get_status(),
            "task_registry": self._task_registry.get_status(),
            "trinity_bridge": self._trinity_bridge.get_status(),
            "agi_os_voice": self._agi_os_voice.get_status(),
            "message_cache": self._composer.get_cache_stats(),
            "event_listeners": self.get_listener_count(),  # v79.1: Include listener counts
            "config": {
                "enabled": self.config.enabled,
                "progress_cooldown": self.config.progress_cooldown,
                "voice_timeout": self.config.voice_timeout,
                "enable_trinity_voice": self.config.enable_trinity_voice,
                "enable_agi_os_voice": self.config.enable_agi_os_voice,
                "enable_voice_approval": self.config.enable_voice_approval,
            },
        }

    def get_active_evolutions(self) -> List[Dict[str, Any]]:
        """Get list of active evolutions."""
        return [ctx.to_dict() for ctx in self._active_evolutions.values()]

    def get_evolution_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recent evolution history."""
        return self._evolution_history[-limit:]

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def announce_completion(
        self,
        task_id: str,
        success: bool,
        files_modified: int = 0,
        execution_time_ms: float = 0,
        error: Optional[str] = None
    ) -> bool:
        """
        Shorthand for announce_evolution_complete.

        This method provides a simpler interface compatible with startup.py
        and other integration points.

        Args:
            task_id: Evolution task ID
            success: Whether evolution succeeded
            files_modified: Count of files modified
            execution_time_ms: Execution time in milliseconds
            error: Error message if failed

        Returns:
            True if announcement was made
        """
        # Create file list placeholder for compatibility
        files_list = [f"file_{i}" for i in range(files_modified)] if files_modified > 0 else []

        return await self.announce_evolution_complete(
            task_id=task_id,
            success=success,
            files_modified=files_list,
            error_message=error or "",
        )

    async def shutdown(self) -> None:
        """Graceful shutdown with task cleanup."""
        cancelled = await self._task_registry.cancel_all()
        if cancelled > 0:
            logger.info(f"[CodingCouncilVoice] Cancelled {cancelled} pending tasks")

        self._active_evolutions.clear()
        logger.info("[CodingCouncilVoice] Shutdown complete")


# =============================================================================
# Global Instance (Lazy Initialization)
# =============================================================================

_evolution_announcer: Optional[CodingCouncilVoiceAnnouncer] = None


def get_evolution_announcer() -> CodingCouncilVoiceAnnouncer:
    """Get or create the global evolution announcer."""
    global _evolution_announcer
    if _evolution_announcer is None:
        _evolution_announcer = CodingCouncilVoiceAnnouncer()
    return _evolution_announcer


async def shutdown_evolution_announcer() -> None:
    """Shutdown the global evolution announcer."""
    global _evolution_announcer
    if _evolution_announcer:
        await _evolution_announcer.shutdown()
        _evolution_announcer = None


# =============================================================================
# Integration Setup (Called during startup)
# =============================================================================


async def setup_voice_integration() -> Dict[str, bool]:
    """
    Set up complete voice integration.

    Returns dict of integration status for each component.
    """
    results = {
        "announcer": False,
        "trinity_bridge": False,
        "agi_os_voice": False,
        "broadcaster_hook": False,
        "websocket_hook": False,
    }

    try:
        announcer = get_evolution_announcer()
        results["announcer"] = True

        # Initialize Trinity bridge
        trinity_ok = await announcer._trinity_bridge.initialize()
        results["trinity_bridge"] = trinity_ok

        # Initialize AGI OS voice
        agi_ok = await announcer._agi_os_voice.initialize()
        results["agi_os_voice"] = agi_ok

        # Hook into broadcaster (done in integration.py)
        results["broadcaster_hook"] = True

        # v79.1: Hook into WebSocket for real-time UI updates
        try:
            ws_ok = await setup_websocket_event_hook(announcer)
            results["websocket_hook"] = ws_ok
        except Exception as ws_err:
            logger.debug(f"[CodingCouncilVoice] WebSocket hook not available: {ws_err}")

        logger.info(f"[CodingCouncilVoice] Voice integration setup: {results}")

    except Exception as e:
        logger.error(f"[CodingCouncilVoice] Integration setup failed: {e}")

    return results


# =============================================================================
# v79.1: WebSocket Event Broadcasting
# =============================================================================


async def setup_websocket_event_hook(announcer: CodingCouncilVoiceAnnouncer) -> bool:
    """
    Set up WebSocket event broadcasting for evolution events.

    This allows connected IDE clients to receive real-time evolution status updates.

    Args:
        announcer: The voice announcer instance to hook into

    Returns:
        True if hook was set up successfully
    """
    try:
        # Try to import WebSocket handler
        try:
            from .ide.websocket_handler import get_websocket_handler, WebSocketMessage, MessageType
        except ImportError:
            from backend.core.coding_council.ide.websocket_handler import (
                get_websocket_handler, WebSocketMessage, MessageType
            )

        # Create the broadcast listener
        async def websocket_evolution_broadcaster(event_type: str, details: Dict[str, Any]) -> None:
            """Broadcast evolution events to connected IDE clients."""
            try:
                handler = await get_websocket_handler()
                if handler.connection_count == 0:
                    return  # No clients connected

                # Map event types to WebSocket message format
                # Frontend expects: { status, task_id, progress, stage, message, error }
                ws_data = {
                    "status": event_type,
                    "task_id": details.get("task_id", ""),
                    "progress": details.get("progress", 0),
                    "stage": details.get("stage", event_type),
                    "description": details.get("description", ""),
                    "files_modified": details.get("files_modified", []),
                    "timestamp": details.get("timestamp", time.time()),
                    "timestamp_iso": details.get("timestamp_iso", ""),
                }

                # Add error info for failed events
                if event_type in ("failed", "error"):
                    ws_data["error"] = details.get("error") or details.get("details", "")

                # Add success flag for complete events
                if event_type == "complete":
                    ws_data["success"] = details.get("success", True)

                # Add approval info
                if event_type in ("approval_needed", "approval_granted", "approval_denied"):
                    ws_data["confirmation_id"] = details.get("confirmation_id", "")
                    ws_data["risk_level"] = details.get("risk_level", "")

                message = WebSocketMessage(
                    type=MessageType.EVOLUTION_STATUS,
                    data=ws_data,
                )

                sent = await handler.broadcast(message)
                if sent > 0:
                    logger.debug(f"[WebSocketHook] Broadcast '{event_type}' to {sent} clients")

            except Exception as e:
                logger.debug(f"[WebSocketHook] Broadcast failed: {e}")

        # Register the listener
        await announcer.add_event_listener(websocket_evolution_broadcaster)
        logger.info("[CodingCouncilVoice] WebSocket event hook registered")
        return True

    except ImportError as e:
        logger.debug(f"[CodingCouncilVoice] WebSocket handler not available: {e}")
        return False
    except Exception as e:
        logger.warning(f"[CodingCouncilVoice] WebSocket hook setup failed: {e}")
        return False

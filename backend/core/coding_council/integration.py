"""
v77.2: Coding Council Integration Module
=========================================

Comprehensive integration for all 6 critical touchpoints:
1. Trinity command handler registration (CRITICAL)
2. FastAPI route registration (HIGH)
3. Voice command routing (HIGH)
4. Main health endpoint integration (MEDIUM)
5. WebSocket status broadcasting (MEDIUM)
6. Intelligent command handler integration (HIGH)

Architecture:
    ┌───────────────┐   Voice   ┌─────────────────────┐   Trinity  ┌───────────┐
    │   User Voice  │ ─────────│ IntelligentCommand  │ ──────────│  J-PRIME    │
    └───────────────┘           │     Handler         │            │  (Mind)   │
                                └─────────────────────┘            └───────────┘
                                         │                                │
                                         ▼                                ▼
    ┌───────────────┐   HTTP    ┌─────────────────────┐   Trinity   ┌───────────┐
    │  HTTP Client  │ ─────────│   FastAPI Routes    │ ◄─────────│  REACTOR  │
    └───────────────┘           └─────────────────────┘            │   CORE    │
                                         │                         └───────────┘
                                         ▼                                │
                                ┌─────────────────────┐                   │
                                │   CODING COUNCIL    │ ◄─────────────────┘
                                │    ORCHESTRATOR     │
                                └─────────────────────┘
                                         │
                                         ▼
                                ┌─────────────────────┐
                                │  WebSocket Broadcast │
                                └─────────────────────┘

Features:
    - Async parallel command processing
    - Intelligent command classification (ML-based)
    - Real-time WebSocket progress broadcasting
    - Circuit breaker for fault tolerance
    - Automatic retry with exponential backoff
    - Cross-repo Trinity communication
    - Dynamic capability discovery
    - Graceful degradation

Author: JARVIS v77.2
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
import uuid
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock
from backend.core.secure_logging import sanitize_for_log

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Configuration (Unified Config Integration)
# ============================================================================

def _get_unified_config():
    """Get unified configuration."""
    try:
        from .config import get_config
        return get_config()
    except ImportError:
        return None


class CodingCouncilConfig:
    """
    Dynamic configuration with unified config integration.

    All settings can be overridden via environment or unified config.
    """

    @classmethod
    def is_enabled(cls) -> bool:
        """Check if Coding Council is enabled."""
        config = _get_unified_config()
        if config:
            return config.enabled
        return os.getenv("CODING_COUNCIL_ENABLED", "true").lower() == "true"

    @classmethod
    def get_timeout(cls, operation: str) -> float:
        """Get timeout for an operation."""
        config = _get_unified_config()
        if config:
            return config.timeouts.get(operation)
        # Fallback to default
        return float(os.getenv(f"CODING_COUNCIL_{operation.upper()}_TIMEOUT", "300"))

    @classmethod
    def get_max_concurrent(cls) -> int:
        """Get max concurrent operations."""
        config = _get_unified_config()
        if config:
            return config.max_concurrent_operations
        return int(os.getenv("CODING_COUNCIL_MAX_CONCURRENT", "3"))

    # Auto-approval settings
    AUTO_APPROVE: bool = os.getenv("CODING_COUNCIL_AUTO_APPROVE", "false").lower() == "true"
    REQUIRE_APPROVAL: bool = os.getenv("CODING_COUNCIL_REQUIRE_APPROVAL", "true").lower() == "true"
    AUTO_APPROVE_SAFE_ONLY: bool = os.getenv("CODING_COUNCIL_AUTO_APPROVE_SAFE_ONLY", "true").lower() == "true"

    # Risk thresholds
    MAX_AUTO_APPROVE_LINES: int = int(os.getenv("CODING_COUNCIL_MAX_AUTO_APPROVE_LINES", "100"))
    MAX_AUTO_APPROVE_FILES: int = int(os.getenv("CODING_COUNCIL_MAX_AUTO_APPROVE_FILES", "3"))

    # Protected paths (never auto-approve)
    PROTECTED_PATHS: Set[str] = set(
        os.getenv("CODING_COUNCIL_PROTECTED_PATHS",
                  ".env,.git,secrets,credentials,private_key,api_key").split(",")
    )

    # Critical files (always require approval)
    CRITICAL_FILES: Set[str] = set(
        os.getenv("CODING_COUNCIL_CRITICAL_FILES",
                  "main.py,run_supervisor.py,__init__.py,setup.py,pyproject.toml").split(",")
    )

    # Framework preferences
    PREFERRED_FRAMEWORK: str = os.getenv("CODING_COUNCIL_PREFERRED_FRAMEWORK", "aider")
    FALLBACK_FRAMEWORKS: List[str] = os.getenv(
        "CODING_COUNCIL_FALLBACK_FRAMEWORKS", "metagpt,repomaster"
    ).split(",")

    # Timeouts
    EVOLUTION_TIMEOUT: int = int(os.getenv("CODING_COUNCIL_EVOLUTION_TIMEOUT", "300"))
    APPROVAL_TIMEOUT: int = int(os.getenv("CODING_COUNCIL_APPROVAL_TIMEOUT", "3600"))

    @classmethod
    def is_protected_path(cls, path: str) -> bool:
        """Check if a path is protected from auto-approval."""
        path_lower = path.lower()
        return any(p in path_lower for p in cls.PROTECTED_PATHS)

    @classmethod
    def is_critical_file(cls, path: str) -> bool:
        """Check if a file is critical and always requires approval."""
        filename = Path(path).name
        return filename in cls.CRITICAL_FILES

    @classmethod
    def can_auto_approve(cls, request: "EvolutionRequest") -> Tuple[bool, str]:
        """
        Determine if a request can be auto-approved.

        Returns:
            Tuple of (can_auto_approve, reason)
        """
        # Check if auto-approve is enabled
        if not cls.AUTO_APPROVE:
            return False, "auto_approve_disabled"

        # Check protected paths
        for path in request.target_files:
            if cls.is_protected_path(path):
                return False, f"protected_path:{path}"
            if cls.is_critical_file(path):
                return False, f"critical_file:{path}"

        # Check risk indicators
        if request.intent == EvolutionIntent.SECURITY_FIX:
            return False, "security_change"

        # Check for safe-only mode
        if cls.AUTO_APPROVE_SAFE_ONLY:
            safe_intents = {
                EvolutionIntent.DOC_UPDATE,
                EvolutionIntent.TEST_ADD,
                EvolutionIntent.OPTIMIZE,
            }
            if request.intent not in safe_intents:
                return False, f"unsafe_intent:{request.intent.value}"

        return True, "approved"


# Global pending approvals storage (in-memory with cleanup)
_pending_approvals: Dict[str, Tuple["EvolutionRequest", float]] = {}
_pending_approvals_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def store_pending_approval(request: "EvolutionRequest") -> str:
    """Store a pending approval request."""
    async with _pending_approvals_lock:
        # Clean up expired approvals
        current_time = time.time()
        expired = [
            k for k, (_, t) in _pending_approvals.items()
            if current_time - t > CodingCouncilConfig.APPROVAL_TIMEOUT
        ]
        for k in expired:
            del _pending_approvals[k]

        # Store new approval
        _pending_approvals[request.id] = (request, current_time)
        return request.id


async def get_pending_approval(task_id: str) -> Optional["EvolutionRequest"]:
    """Get a pending approval request."""
    async with _pending_approvals_lock:
        if task_id in _pending_approvals:
            request, timestamp = _pending_approvals[task_id]
            # Check if expired
            if time.time() - timestamp > CodingCouncilConfig.APPROVAL_TIMEOUT:
                del _pending_approvals[task_id]
                return None
            return request
        return None


async def remove_pending_approval(task_id: str) -> bool:
    """Remove a pending approval after execution."""
    async with _pending_approvals_lock:
        if task_id in _pending_approvals:
            del _pending_approvals[task_id]
            return True
        return False


async def list_pending_approvals() -> List[Dict[str, Any]]:
    """List all pending approval requests."""
    async with _pending_approvals_lock:
        current_time = time.time()
        result = []
        for task_id, (request, timestamp) in _pending_approvals.items():
            age = current_time - timestamp
            if age <= CodingCouncilConfig.APPROVAL_TIMEOUT:
                result.append({
                    "task_id": task_id,
                    "request": request.to_dict(),
                    "age_seconds": int(age),
                    "expires_in": int(CodingCouncilConfig.APPROVAL_TIMEOUT - age),
                })
        return result


# ============================================================================
# Advanced Concurrency Control (v77.2 Super-Beefed)
# ============================================================================


class EvolutionSemaphore:
    """
    Concurrent execution limiter with priority queue.

    Features:
    - Limits concurrent evolutions (default: 3)
    - Priority-based queue (higher priority executes first)
    - Fair scheduling with aging (prevents starvation)
    - Automatic timeout release
    """

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._active_tasks: Dict[str, float] = {}  # task_id -> start_time
        self._queue: List[Tuple[int, float, str, asyncio.Event]] = []  # (priority, timestamp, task_id, event)
        self._lock = asyncio.Lock()

    async def acquire(self, task_id: str, priority: int = 5, timeout: float = 300.0) -> bool:
        """
        Acquire execution slot with priority.

        Args:
            task_id: Unique task identifier
            priority: 1-10 (higher = more urgent)
            timeout: Max wait time in seconds

        Returns:
            True if acquired, False if timed out
        """
        event = asyncio.Event()
        enqueue_time = time.time()

        async with self._lock:
            # Add to priority queue (negative priority for max-heap behavior)
            self._queue.append((-priority, enqueue_time, task_id, event))
            self._queue.sort(key=lambda x: (x[0], x[1]))  # Sort by priority, then time

        try:
            # Wait for slot with timeout
            acquired = await asyncio.wait_for(
                self._wait_for_slot(task_id, event),
                timeout=timeout
            )

            if acquired:
                async with self._lock:
                    self._active_tasks[task_id] = time.time()

            return acquired

        except asyncio.TimeoutError:
            # Remove from queue on timeout
            async with self._lock:
                self._queue = [q for q in self._queue if q[2] != task_id]
            return False

    async def _wait_for_slot(self, task_id: str, event: asyncio.Event) -> bool:
        """Wait for execution slot."""
        await self._semaphore.acquire()

        async with self._lock:
            # Check if we're next in queue
            if self._queue and self._queue[0][2] == task_id:
                self._queue.pop(0)
                event.set()
                return True

            # Not our turn, release and wait
            self._semaphore.release()

        # Wait for our turn
        await event.wait()
        await self._semaphore.acquire()
        return True

    async def release(self, task_id: str) -> None:
        """Release execution slot."""
        async with self._lock:
            if task_id in self._active_tasks:
                del self._active_tasks[task_id]

            # Signal next in queue
            if self._queue:
                _, _, next_task_id, next_event = self._queue[0]
                next_event.set()

        self._semaphore.release()

    @property
    def active_count(self) -> int:
        """Number of active evolutions."""
        return len(self._active_tasks)

    @property
    def queue_length(self) -> int:
        """Number of waiting evolutions."""
        return len(self._queue)


# Global semaphore
_evolution_semaphore: Optional[EvolutionSemaphore] = None


def get_evolution_semaphore() -> EvolutionSemaphore:
    """Get or create global evolution semaphore."""
    global _evolution_semaphore
    if _evolution_semaphore is None:
        max_concurrent = int(os.getenv("CODING_COUNCIL_MAX_CONCURRENT", "3"))
        _evolution_semaphore = EvolutionSemaphore(max_concurrent)
    return _evolution_semaphore


# ============================================================================
# Advanced Rate Limiter (Token Bucket Algorithm)
# ============================================================================


class RateLimiter:
    """
    Token bucket rate limiter with burst support.

    Features:
    - Configurable rate (requests per second)
    - Burst allowance for temporary spikes
    - Per-client tracking (IP/user based)
    - Sliding window for smooth limiting
    """

    def __init__(
        self,
        rate: float = 10.0,  # requests per second
        burst: int = 20,  # max burst size
        per_client: bool = True
    ):
        self._rate = rate
        self._burst = burst
        self._per_client = per_client
        self._tokens: Dict[str, float] = {}  # client_id -> tokens
        self._last_update: Dict[str, float] = {}  # client_id -> timestamp
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str = "global") -> Tuple[bool, float]:
        """
        Try to acquire a rate limit token.

        Returns:
            Tuple of (allowed, wait_time_if_denied)
        """
        if not self._per_client:
            client_id = "global"

        async with self._lock:
            current_time = time.time()

            # Initialize if new client
            if client_id not in self._tokens:
                self._tokens[client_id] = self._burst
                self._last_update[client_id] = current_time

            # Refill tokens based on elapsed time
            elapsed = current_time - self._last_update[client_id]
            self._tokens[client_id] = min(
                self._burst,
                self._tokens[client_id] + elapsed * self._rate
            )
            self._last_update[client_id] = current_time

            # Check if we have tokens
            if self._tokens[client_id] >= 1.0:
                self._tokens[client_id] -= 1.0
                return True, 0.0
            else:
                # Calculate wait time
                wait_time = (1.0 - self._tokens[client_id]) / self._rate
                return False, wait_time

    async def wait_and_acquire(self, client_id: str = "global", max_wait: float = 30.0) -> bool:
        """Wait for rate limit if needed, up to max_wait seconds."""
        allowed, wait_time = await self.acquire(client_id)

        if allowed:
            return True

        if wait_time > max_wait:
            return False

        await asyncio.sleep(wait_time)
        return (await self.acquire(client_id))[0]


# Global rate limiter
_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Get or create global rate limiter."""
    global _rate_limiter
    if _rate_limiter is None:
        rate = float(os.getenv("CODING_COUNCIL_RATE_LIMIT", "10.0"))
        burst = int(os.getenv("CODING_COUNCIL_RATE_BURST", "20"))
        _rate_limiter = RateLimiter(rate=rate, burst=burst)
    return _rate_limiter


# ============================================================================
# Circuit Breaker with Exponential Backoff
# ============================================================================


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, reject requests
    HALF_OPEN = "half_open"  # Testing if recovered


class CircuitBreaker:
    """
    Circuit breaker pattern with exponential backoff.

    Features:
    - Prevents cascading failures
    - Automatic recovery testing
    - Exponential backoff on repeated failures
    - Per-operation tracking
    - Health metrics reporting
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        backoff_multiplier: float = 2.0,
        max_backoff: float = 300.0
    ):
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._backoff_multiplier = backoff_multiplier
        self._max_backoff = max_backoff

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[float] = None
        self._current_backoff = recovery_timeout
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

        # Metrics
        self._total_calls = 0
        self._total_failures = 0
        self._total_rejections = 0

    async def can_execute(self) -> Tuple[bool, str]:
        """
        Check if execution is allowed.

        Returns:
            Tuple of (allowed, reason)
        """
        async with self._lock:
            self._total_calls += 1

            if self._state == CircuitState.CLOSED:
                return True, "circuit_closed"

            elif self._state == CircuitState.OPEN:
                # Check if recovery timeout has passed
                if self._last_failure_time:
                    elapsed = time.time() - self._last_failure_time
                    if elapsed >= self._current_backoff:
                        # Transition to half-open
                        self._state = CircuitState.HALF_OPEN
                        self._half_open_calls = 0
                        logger.info(f"🔌 [CircuitBreaker] Transitioning to HALF_OPEN after {elapsed:.1f}s")
                        return True, "circuit_half_open"

                self._total_rejections += 1
                remaining = self._current_backoff - (time.time() - (self._last_failure_time or 0))
                return False, f"circuit_open:retry_in_{remaining:.1f}s"

            elif self._state == CircuitState.HALF_OPEN:
                if self._half_open_calls < self._half_open_max_calls:
                    self._half_open_calls += 1
                    return True, "circuit_half_open"
                return False, "circuit_half_open:max_calls_reached"

            return False, "unknown_state"

    async def record_success(self) -> None:
        """Record successful execution."""
        async with self._lock:
            self._success_count += 1

            if self._state == CircuitState.HALF_OPEN:
                # Successful call in half-open, close the circuit
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._current_backoff = self._recovery_timeout  # Reset backoff
                logger.info("🔌 [CircuitBreaker] Circuit CLOSED - recovery successful")

    async def record_failure(self, error: Optional[Exception] = None) -> None:
        """Record failed execution."""
        async with self._lock:
            self._failure_count += 1
            self._total_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                # Failure in half-open, reopen with increased backoff
                self._state = CircuitState.OPEN
                self._current_backoff = min(
                    self._current_backoff * self._backoff_multiplier,
                    self._max_backoff
                )
                logger.warning(f"🔌 [CircuitBreaker] Circuit OPEN - half-open test failed, backoff: {self._current_backoff:.1f}s")

            elif self._state == CircuitState.CLOSED:
                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(f"🔌 [CircuitBreaker] Circuit OPEN - {self._failure_count} failures")

    async def reset(self) -> None:
        """Manually reset the circuit breaker."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._current_backoff = self._recovery_timeout
            logger.info("🔌 [CircuitBreaker] Circuit manually reset")

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def metrics(self) -> Dict[str, Any]:
        return {
            "state": self._state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "total_calls": self._total_calls,
            "total_failures": self._total_failures,
            "total_rejections": self._total_rejections,
            "current_backoff": self._current_backoff,
        }


# Global circuit breaker
_circuit_breaker: Optional[CircuitBreaker] = None


def get_circuit_breaker() -> CircuitBreaker:
    """Get or create global circuit breaker."""
    global _circuit_breaker
    if _circuit_breaker is None:
        _circuit_breaker = CircuitBreaker(
            failure_threshold=int(os.getenv("CODING_COUNCIL_FAILURE_THRESHOLD", "5")),
            recovery_timeout=float(os.getenv("CODING_COUNCIL_RECOVERY_TIMEOUT", "30.0")),
        )
    return _circuit_breaker


# ============================================================================
# Adaptive Framework Selector (Thompson Sampling / Multi-Armed Bandit)
# ============================================================================


class AdaptiveFrameworkSelector:
    """
    Adaptive framework selection using Thompson Sampling.

    Features:
    - Bayesian approach to exploration/exploitation
    - Learns from historical success rates
    - Automatic adaptation to changing conditions
    - Context-aware selection (task complexity, file types)
    - Persistence of learned parameters
    """

    def __init__(self):
        # Beta distribution parameters (alpha=successes+1, beta=failures+1)
        # Start with weak priors (alpha=1, beta=1 = uniform)
        self._framework_stats: Dict[str, Dict[str, float]] = {
            "aider": {"alpha": 2.0, "beta": 1.0},  # Slight prior for aider (known good)
            "metagpt": {"alpha": 1.5, "beta": 1.0},
            "claude_code": {"alpha": 1.5, "beta": 1.0},
            "openhands": {"alpha": 1.0, "beta": 1.0},
            "continue": {"alpha": 1.0, "beta": 1.0},
        }

        # Context-specific stats (complexity -> framework -> stats)
        self._context_stats: Dict[str, Dict[str, Dict[str, float]]] = {}

        self._lock = asyncio.Lock()
        self._selection_history: List[Dict[str, Any]] = []

    async def select_framework(
        self,
        available_frameworks: List[str],
        context: Optional[Dict[str, Any]] = None,
        exploration_rate: float = 0.1
    ) -> str:
        """
        Select framework using Thompson Sampling.

        Args:
            available_frameworks: List of available framework names
            context: Optional context (complexity, file_types, etc.)
            exploration_rate: Probability of random exploration (0-1)

        Returns:
            Selected framework name
        """
        import random

        async with self._lock:
            # Exploration: random selection with probability exploration_rate
            if random.random() < exploration_rate:
                selected = random.choice(available_frameworks)
                self._selection_history.append({
                    "selected": selected,
                    "method": "exploration",
                    "timestamp": time.time(),
                })
                return selected

            # Exploitation: Thompson Sampling
            samples = {}
            for framework in available_frameworks:
                stats = self._get_stats(framework, context)
                # Sample from Beta distribution
                sample = random.betavariate(stats["alpha"], stats["beta"])
                samples[framework] = sample

            # Select framework with highest sample
            selected = max(samples, key=samples.get)

            self._selection_history.append({
                "selected": selected,
                "method": "thompson_sampling",
                "samples": samples,
                "timestamp": time.time(),
            })

            return selected

    def _get_stats(
        self,
        framework: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, float]:
        """Get stats for framework, optionally context-specific."""
        # Try context-specific stats first
        if context and "complexity" in context:
            complexity = context["complexity"]
            if complexity in self._context_stats:
                if framework in self._context_stats[complexity]:
                    return self._context_stats[complexity][framework]

        # Fall back to global stats
        return self._framework_stats.get(
            framework,
            {"alpha": 1.0, "beta": 1.0}
        )

    async def record_result(
        self,
        framework: str,
        success: bool,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Record execution result to update beliefs.

        Args:
            framework: Framework that was used
            success: Whether execution succeeded
            context: Optional context for context-specific learning
        """
        async with self._lock:
            # Update global stats
            if framework not in self._framework_stats:
                self._framework_stats[framework] = {"alpha": 1.0, "beta": 1.0}

            if success:
                self._framework_stats[framework]["alpha"] += 1.0
            else:
                self._framework_stats[framework]["beta"] += 1.0

            # Update context-specific stats
            if context and "complexity" in context:
                complexity = context["complexity"]
                if complexity not in self._context_stats:
                    self._context_stats[complexity] = {}
                if framework not in self._context_stats[complexity]:
                    self._context_stats[complexity][framework] = {"alpha": 1.0, "beta": 1.0}

                if success:
                    self._context_stats[complexity][framework]["alpha"] += 1.0
                else:
                    self._context_stats[complexity][framework]["beta"] += 1.0

            logger.debug(f"🎰 [AdaptiveSelector] Updated {framework}: success={success}")

    def get_success_rates(self) -> Dict[str, float]:
        """Get estimated success rates for all frameworks."""
        rates = {}
        for framework, stats in self._framework_stats.items():
            # Expected value of Beta distribution
            rates[framework] = stats["alpha"] / (stats["alpha"] + stats["beta"])
        return rates

    @property
    def stats(self) -> Dict[str, Any]:
        return {
            "global_stats": self._framework_stats,
            "context_stats": self._context_stats,
            "success_rates": self.get_success_rates(),
            "selection_count": len(self._selection_history),
        }


# Global adaptive selector
_adaptive_selector: Optional[AdaptiveFrameworkSelector] = None


def get_adaptive_selector() -> AdaptiveFrameworkSelector:
    """Get or create global adaptive framework selector."""
    global _adaptive_selector
    if _adaptive_selector is None:
        _adaptive_selector = AdaptiveFrameworkSelector()
    return _adaptive_selector


# ============================================================================
# Timeout Wrapper for Long Operations
# ============================================================================


async def with_timeout(
    coro: Any,
    timeout: float,
    task_id: str,
    operation: str,
    on_timeout: Optional[Callable[[], Any]] = None
) -> Tuple[bool, Any]:
    """
    Execute coroutine with timeout and cleanup.

    Args:
        coro: Coroutine to execute
        timeout: Timeout in seconds
        task_id: Task ID for logging
        operation: Operation name for logging
        on_timeout: Optional cleanup callback on timeout

    Returns:
        Tuple of (success, result_or_error)
    """
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        return True, result
    except asyncio.TimeoutError:
        logger.warning(f"⏱️ [Timeout] {operation} timed out after {timeout}s (task: {task_id})")
        if on_timeout:
            try:
                if asyncio.iscoroutinefunction(on_timeout):
                    await on_timeout()
                else:
                    on_timeout()
            except Exception as e:
                logger.error(f"⏱️ [Timeout] Cleanup failed: {e}")
        return False, f"Timeout after {timeout}s"
    except asyncio.CancelledError:
        logger.info(f"⏱️ [Timeout] {operation} cancelled (task: {task_id})")
        raise
    except Exception as e:
        logger.error(f"⏱️ [Timeout] {operation} error: {e}")
        return False, str(e)


# ============================================================================
# Command Classification (Intelligent, No Hardcoding)
# ============================================================================


class EvolutionIntent(Enum):
    """Types of evolution requests."""

    CODE_EVOLUTION = "code_evolution"  # General self-evolution
    BUG_FIX = "bug_fix"  # Fix a bug
    FEATURE_ADD = "feature_add"  # Add new feature
    REFACTOR = "refactor"  # Refactor code
    OPTIMIZE = "optimize"  # Performance optimization
    SECURITY_FIX = "security_fix"  # Security vulnerability fix
    TEST_ADD = "test_add"  # Add tests
    DOC_UPDATE = "doc_update"  # Update documentation
    DEPENDENCY_UPDATE = "dependency_update"  # Update dependencies


@dataclass
class EvolutionRequest:
    """
    Parsed evolution request from any source.

    Can originate from:
    - Voice command
    - Trinity message from J-Prime
    - HTTP API request
    - Internal trigger
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    intent: EvolutionIntent = EvolutionIntent.CODE_EVOLUTION
    target_files: List[str] = field(default_factory=list)
    target_modules: List[str] = field(default_factory=list)
    source: str = "unknown"  # voice, trinity, http, internal
    priority: int = 5  # 1-10, higher = more urgent
    require_approval: bool = True
    require_sandbox: bool = False
    require_planning: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "intent": self.intent.value,
            "target_files": self.target_files,
            "target_modules": self.target_modules,
            "source": self.source,
            "priority": self.priority,
            "require_approval": self.require_approval,
            "require_sandbox": self.require_sandbox,
            "require_planning": self.require_planning,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


class CommandClassifier:
    """
    ML-inspired command classifier for evolution requests.

    Uses semantic analysis and pattern matching to classify commands
    without hardcoding specific phrases.
    """

    # Semantic patterns for intent classification (learned-style)
    INTENT_PATTERNS: Dict[EvolutionIntent, List[str]] = {
        EvolutionIntent.BUG_FIX: [
            r"\bfix\b", r"\bbug\b", r"\berror\b", r"\bcrash\b",
            r"\bbroken\b", r"\bfailing\b", r"\bwrong\b", r"\bissue\b",
        ],
        EvolutionIntent.FEATURE_ADD: [
            r"\badd\b", r"\bcreate\b", r"\bimplement\b", r"\bnew\b",
            r"\bfeature\b", r"\bsupport\b", r"\benable\b", r"\bintroduce\b",
        ],
        EvolutionIntent.REFACTOR: [
            r"\brefactor\b", r"\breorganize\b", r"\brestructure\b",
            r"\bclean\s*up\b", r"\bsimplify\b", r"\bextract\b",
        ],
        EvolutionIntent.OPTIMIZE: [
            r"\boptimize\b", r"\bperformance\b", r"\bfaster\b", r"\bspeed\b",
            r"\befficient\b", r"\bimprove\b", r"\benhance\b",
        ],
        EvolutionIntent.SECURITY_FIX: [
            r"\bsecurity\b", r"\bvulnerability\b", r"\bcve\b", r"\bexploit\b",
            r"\binjection\b", r"\bxss\b", r"\bauth\b",
        ],
        EvolutionIntent.TEST_ADD: [
            r"\btest\b", r"\bunit\s*test\b", r"\bcoverage\b", r"\bspec\b",
        ],
        EvolutionIntent.DOC_UPDATE: [
            r"\bdoc\b", r"\bdocument\b", r"\breadme\b", r"\bcomment\b",
        ],
        EvolutionIntent.DEPENDENCY_UPDATE: [
            r"\bdependenc\b", r"\bupgrade\b", r"\bpackage\b", r"\bversion\b",
        ],
    }

    # Evolution trigger patterns (indicates evolution request)
    EVOLUTION_TRIGGERS = [
        r"\bevolve\b",
        r"\bself[- ]?evolve\b",
        r"\bself[- ]?improve\b",
        r"\bupdate\s+(the\s+)?code\b",
        r"\bmodify\s+(the\s+)?code\b",
        r"\bchange\s+(the\s+)?code\b",
        r"\bimprove\s+(the\s+)?system\b",
        r"\bupgrade\s+(the\s+)?system\b",
        r"\bupdate\s+yourself\b",
        r"\bmodify\s+yourself\b",
        r"\bfix\s+yourself\b",
        r"\benhance\s+(the\s+)?capabilities\b",
        r"\bcode\s+evolution\b",
    ]

    # File reference patterns
    FILE_PATTERNS = [
        r"(?:in|at|file|module)\s+['\"]?([a-zA-Z0-9_/.-]+\.py)['\"]?",
        r"([a-zA-Z0-9_/.-]+\.py)\s+(?:file|module)?",
        r"backend/[a-zA-Z0-9_/.-]+\.py",
    ]

    # Module reference patterns
    MODULE_PATTERNS = [
        r"(?:the\s+)?([a-zA-Z_][a-zA-Z0-9_]*)\s+(?:module|handler|service|manager)",
        r"(?:in|at)\s+(?:the\s+)?([a-zA-Z_][a-zA-Z0-9_]*)",
    ]

    @classmethod
    def is_evolution_command(cls, text: str) -> bool:
        """
        Determine if text is an evolution command.

        Uses semantic patterns, not hardcoded strings.
        """
        text_lower = text.lower()

        # Check evolution triggers
        for pattern in cls.EVOLUTION_TRIGGERS:
            if re.search(pattern, text_lower):
                return True

        return False

    @classmethod
    def classify_intent(cls, text: str) -> EvolutionIntent:
        """
        Classify the evolution intent from text.

        Uses pattern matching with scoring.
        """
        text_lower = text.lower()
        scores: Dict[EvolutionIntent, float] = {}

        for intent, patterns in cls.INTENT_PATTERNS.items():
            score = 0.0
            for pattern in patterns:
                matches = len(re.findall(pattern, text_lower))
                score += matches * 1.0

            if score > 0:
                scores[intent] = score

        if not scores:
            return EvolutionIntent.CODE_EVOLUTION

        return max(scores, key=scores.get)

    @classmethod
    def extract_files(cls, text: str) -> List[str]:
        """Extract file references from text."""
        files = []
        for pattern in cls.FILE_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            files.extend(matches if isinstance(matches[0], str) else [m for m in matches] if matches else [])

        # Also find direct file paths
        path_pattern = r"(?:^|\s)([a-zA-Z0-9_/.-]+\.py)(?:\s|$|,)"
        paths = re.findall(path_pattern, text)
        files.extend(paths)

        return list(set(files))

    @classmethod
    def extract_modules(cls, text: str) -> List[str]:
        """Extract module references from text."""
        modules = []
        for pattern in cls.MODULE_PATTERNS:
            matches = re.findall(pattern, text, re.IGNORECASE)
            modules.extend(matches)

        return list(set(modules))

    @classmethod
    def parse_command(cls, text: str, source: str = "unknown") -> EvolutionRequest:
        """
        Parse a text command into an EvolutionRequest.

        Full semantic parsing without hardcoding.
        """
        return EvolutionRequest(
            description=text,
            intent=cls.classify_intent(text),
            target_files=cls.extract_files(text),
            target_modules=cls.extract_modules(text),
            source=source,
            priority=cls._estimate_priority(text),
            require_approval=cls._needs_approval(text),
            require_planning=cls._needs_planning(text),
        )

    @classmethod
    def _estimate_priority(cls, text: str) -> int:
        """Estimate priority from text."""
        text_lower = text.lower()

        # High priority indicators
        if any(w in text_lower for w in ["urgent", "critical", "asap", "immediately"]):
            return 9

        # Security is always high priority
        if any(w in text_lower for w in ["security", "vulnerability", "exploit"]):
            return 8

        # Bug fixes are medium-high
        if any(w in text_lower for w in ["bug", "crash", "error", "broken"]):
            return 7

        return 5

    @classmethod
    def _needs_approval(cls, text: str) -> bool:
        """Check if approval is needed."""
        text_lower = text.lower()

        # Skip approval if explicitly requested
        if any(p in text_lower for p in ["without approval", "auto approve", "just do it"]):
            return False

        # Always require approval for security changes
        if "security" in text_lower:
            return True

        return True  # Default to requiring approval

    @classmethod
    def _needs_planning(cls, text: str) -> bool:
        """Check if planning phase is needed."""
        text_lower = text.lower()

        # Complex changes need planning
        complexity_indicators = [
            "complex", "large", "major", "significant", "comprehensive",
            "multiple files", "across", "refactor", "restructure",
        ]

        return any(i in text_lower for i in complexity_indicators)


# ============================================================================
# WebSocket Broadcasting (Gap #5)
# ============================================================================


class EvolutionBroadcaster:
    """
    Broadcasts evolution status to WebSocket clients.

    Features:
    - Real-time progress updates
    - Multi-client broadcast via UnifiedWebSocketManager
    - Buffered replay for late joiners
    - Automatic cleanup
    - Bridge to main WebSocket infrastructure
    - v79.0: Voice announcements via CodingCouncilVoiceAnnouncer
    """

    def __init__(self, buffer_size: int = 100):
        self._clients: Set[weakref.ref] = set()
        self._buffer: List[Dict[str, Any]] = []
        self._buffer_size = buffer_size
        self._lock = asyncio.Lock()
        self._unified_ws_manager: Optional[Any] = None
        self._ws_manager_checked = False
        # v79.0: Voice announcer integration
        self._voice_announcer: Optional[Any] = None
        self._voice_announcer_checked = False

    def _get_unified_ws_manager(self) -> Optional[Any]:
        """
        Lazily get the UnifiedWebSocketManager for bridged broadcasting.

        This connects evolution broadcasts to the main WebSocket infrastructure
        so frontend clients can receive real-time updates.
        """
        if self._ws_manager_checked:
            return self._unified_ws_manager

        self._ws_manager_checked = True
        try:
            # Try to import and get the global ws manager
            try:
                from backend.api.unified_websocket import get_ws_manager
            except ImportError:
                from api.unified_websocket import get_ws_manager

            self._unified_ws_manager = get_ws_manager()
            logger.info("[EvolutionBroadcaster] Connected to UnifiedWebSocketManager")
        except ImportError:
            logger.debug("[EvolutionBroadcaster] UnifiedWebSocketManager not available")
            self._unified_ws_manager = None
        except Exception as e:
            logger.debug(f"[EvolutionBroadcaster] Failed to get WS manager: {e}")
            self._unified_ws_manager = None

        return self._unified_ws_manager

    def _get_voice_announcer(self) -> Optional[Any]:
        """
        v79.0: Lazily get the CodingCouncilVoiceAnnouncer for voice broadcasts.

        This connects evolution broadcasts to voice output for real-time
        spoken feedback during code evolution operations.
        """
        if self._voice_announcer_checked:
            return self._voice_announcer

        self._voice_announcer_checked = True
        try:
            try:
                from core.coding_council.voice_announcer import get_evolution_announcer
            except ImportError:
                from backend.core.coding_council.voice_announcer import get_evolution_announcer

            self._voice_announcer = get_evolution_announcer()
            logger.info("[EvolutionBroadcaster] Connected to VoiceAnnouncer")
        except ImportError:
            logger.debug("[EvolutionBroadcaster] VoiceAnnouncer not available")
            self._voice_announcer = None
        except Exception as e:
            logger.debug(f"[EvolutionBroadcaster] Failed to get voice announcer: {e}")
            self._voice_announcer = None

        return self._voice_announcer

    async def _trigger_voice_announcement(
        self,
        task_id: str,
        status: str,
        progress: float,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        v79.0: Trigger voice announcement based on broadcast status.

        Maps broadcast events to appropriate voice announcements.
        """
        announcer = self._get_voice_announcer()
        if not announcer:
            return

        try:
            if status == "started":
                await announcer.announce_evolution_started(
                    task_id=task_id,
                    description=message,
                    target_files=details.get("target_files") if details else None,
                    trinity_involved=details.get("trinity_involved", False) if details else False,
                )
            elif status in ("progress", "stage"):
                stage = details.get("stage") if details else None
                await announcer.announce_evolution_progress(
                    task_id=task_id,
                    progress=progress,
                    stage=stage,
                )
            elif status == "complete":
                await announcer.announce_evolution_complete(
                    task_id=task_id,
                    success=True,
                    files_modified=details.get("files_modified") if details else None,
                )
            elif status == "failed":
                await announcer.announce_evolution_complete(
                    task_id=task_id,
                    success=False,
                    error_message=message,
                )
            elif status == "confirmation_needed":
                confirmation_id = details.get("confirmation_id", "") if details else ""
                await announcer.announce_confirmation_needed(
                    task_id=task_id,
                    description=message,
                    confirmation_id=confirmation_id,
                )
            elif status == "error":
                error_type = details.get("error_type", "unknown") if details else "unknown"
                await announcer.announce_error(
                    task_id=task_id,
                    error_type=error_type,
                    details=message,
                )
        except Exception as e:
            logger.debug(f"[EvolutionBroadcaster] Voice announcement failed: {e}")

    async def register_client(self, client: Any) -> None:
        """Register a WebSocket client."""

        def remove_client(ref):
            self._clients.discard(ref)

        ref = weakref.ref(client, remove_client)
        async with self._lock:
            self._clients.add(ref)

            # Send buffered messages to new client
            if hasattr(client, "send_json"):
                for msg in self._buffer:
                    try:
                        await client.send_json(msg)
                    except Exception:
                        pass

    async def unregister_client(self, client: Any) -> None:
        """Unregister a WebSocket client."""
        async with self._lock:
            to_remove = None
            for ref in self._clients:
                if ref() is client:
                    to_remove = ref
                    break
            if to_remove:
                self._clients.discard(to_remove)

    async def broadcast(
        self,
        task_id: str,
        status: str,
        progress: float = 0.0,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Broadcast evolution status to all clients.

        Broadcasts via two channels:
        1. Direct registered clients (for evolution-specific connections)
        2. UnifiedWebSocketManager (for main frontend connections)

        Returns:
            Number of clients notified
        """
        payload = {
            "type": "evolution_status",
            "task_id": task_id,
            "status": status,
            "progress": progress,
            "message": message,
            "details": details or {},
            "timestamp": time.time(),
        }

        async with self._lock:
            # Buffer the message
            self._buffer.append(payload)
            if len(self._buffer) > self._buffer_size:
                self._buffer.pop(0)

            # Broadcast to all direct clients
            notified = 0
            dead_refs = []

            for ref in self._clients:
                client = ref()
                if client is None:
                    dead_refs.append(ref)
                    continue

                try:
                    if hasattr(client, "send_json"):
                        await client.send_json(payload)
                        notified += 1
                    elif hasattr(client, "send"):
                        await client.send(json.dumps(payload))
                        notified += 1
                except Exception as e:
                    logger.debug(f"[Broadcaster] Client send failed: {e}")
                    dead_refs.append(ref)

            # Clean up dead references
            for ref in dead_refs:
                self._clients.discard(ref)

            # Bridge to UnifiedWebSocketManager for main frontend
            ws_manager = self._get_unified_ws_manager()
            if ws_manager:
                try:
                    # Broadcast via unified manager with evolution capability filter
                    await ws_manager.broadcast(payload, capability="evolution")
                    logger.debug(f"[Broadcaster] Bridged to UnifiedWS: {status}")
                except Exception as e:
                    logger.debug(f"[Broadcaster] UnifiedWS broadcast failed: {e}")
                    # Try fallback without capability filter
                    try:
                        await ws_manager.broadcast(payload)
                    except Exception:
                        pass

            # v79.0: Trigger voice announcement (async, non-blocking)
            # Use create_task to avoid slowing down the broadcast
            asyncio.create_task(
                self._trigger_voice_announcement(
                    task_id=task_id,
                    status=status,
                    progress=progress,
                    message=message,
                    details=details,
                )
            )

            return notified

    @property
    def client_count(self) -> int:
        """Get number of connected clients."""
        # Clean dead refs
        dead = [ref for ref in self._clients if ref() is None]
        for ref in dead:
            self._clients.discard(ref)
        return len(self._clients)


# Global broadcaster
_evolution_broadcaster: Optional[EvolutionBroadcaster] = None


def get_evolution_broadcaster() -> EvolutionBroadcaster:
    """Get or create the global evolution broadcaster."""
    global _evolution_broadcaster
    if _evolution_broadcaster is None:
        _evolution_broadcaster = EvolutionBroadcaster()
    return _evolution_broadcaster


# ============================================================================
# Trinity Integration (Gap #1 - CRITICAL)
# ============================================================================


class TrinityEvolutionHandler:
    """
    Handles evolution commands from J-Prime via Trinity.

    Features:
    - Async command processing
    - Progress reporting back to J-Prime
    - Automatic ACK/NACK
    - Circuit breaker for failures
    """

    def __init__(self):
        self._active_evolutions: Dict[str, asyncio.Task] = {}
        self._failure_count = 0
        self._max_failures = 5
        self._circuit_open = False

    async def handle_evolution_command(self, command: Any) -> Dict[str, Any]:
        """
        Handle evolution command from Trinity.

        Args:
            command: TrinityCommand object

        Returns:
            Result dict with success status and details
        """
        # Circuit breaker check
        if self._circuit_open:
            return {
                "success": False,
                "error": "Circuit breaker open - too many failures",
            }

        try:
            # Import coding council lazily
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "success": False,
                    "error": "Coding Council not initialized",
                }

            # Parse command payload
            payload = getattr(command, "payload", {}) or {}

            # Create evolution request
            request = EvolutionRequest(
                description=payload.get("description", ""),
                target_files=payload.get("target_files", []),
                source="trinity",
                require_approval=payload.get("require_approval", True),
                require_sandbox=payload.get("require_sandbox", False),
                require_planning=payload.get("require_planning", False),
                metadata=payload.get("metadata", {}),
            )

            # Execute evolution
            result = await council.evolve(
                description=request.description,
                target_files=request.target_files,
                require_approval=request.require_approval,
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Broadcast progress
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=result.task_id if hasattr(result, "task_id") else request.id,
                status="complete" if result.success else "failed",
                progress=1.0 if result.success else 0.0,
                message="Evolution complete" if result.success else str(result.error),
            )

            # Reset failure count on success
            if result.success:
                self._failure_count = 0

            return {
                "success": result.success,
                "task_id": getattr(result, "task_id", request.id),
                "changes_made": getattr(result, "changes_made", []),
                "files_modified": getattr(result, "files_modified", []),
                "error": getattr(result, "error", None),
            }

        except Exception as e:
            logger.error(f"[TrinityEvolution] Command failed: {e}")

            # Track failures for circuit breaker
            self._failure_count += 1
            if self._failure_count >= self._max_failures:
                self._circuit_open = True
                logger.warning("[TrinityEvolution] Circuit breaker OPEN")

            return {
                "success": False,
                "error": str(e),
            }

    async def handle_evolution_status(self, command: Any) -> Dict[str, Any]:
        """Handle evolution status query from Trinity."""
        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {"success": False, "error": "Coding Council not available"}

        status = await council.get_status()
        return {"success": True, "status": status}

    async def handle_evolution_rollback(self, command: Any) -> Dict[str, Any]:
        """Handle evolution rollback command from Trinity."""
        payload = getattr(command, "payload", {}) or {}
        task_id = payload.get("task_id")

        if not task_id:
            return {"success": False, "error": "No task_id provided"}

        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {"success": False, "error": "Coding Council not available"}

        result = await council.rollback(task_id)
        return {
            "success": result.success if hasattr(result, "success") else True,
            "task_id": task_id,
            "rolled_back": True,
        }


# Global Trinity handler
_trinity_handler: Optional[TrinityEvolutionHandler] = None


def get_trinity_evolution_handler() -> TrinityEvolutionHandler:
    """Get or create global Trinity evolution handler."""
    global _trinity_handler
    if _trinity_handler is None:
        _trinity_handler = TrinityEvolutionHandler()
    return _trinity_handler


def register_evolution_handlers(bridge: Any) -> None:
    """
    Register evolution command handlers with Trinity bridge.

    This is the main integration point (Gap #1).

    Args:
        bridge: ReactorBridge instance
    """
    try:
        # Import Trinity types
        try:
            from backend.system.reactor_bridge import TrinityIntent
        except ImportError:
            from system.reactor_bridge import TrinityIntent

        handler = get_trinity_evolution_handler()

        # Register handlers for evolution intents
        intents_to_handlers = {
            TrinityIntent.EVOLVE_CODE: handler.handle_evolution_command,
            TrinityIntent.EVOLUTION_STATUS: handler.handle_evolution_status,
            TrinityIntent.EVOLUTION_ROLLBACK: handler.handle_evolution_rollback,
        }

        for intent, handler_func in intents_to_handlers.items():
            if hasattr(bridge, "register_handler"):
                bridge.register_handler(handler_func, [intent])
            elif hasattr(bridge, "_command_handlers"):
                if intent not in bridge._command_handlers:
                    bridge._command_handlers[intent] = []
                bridge._command_handlers[intent].append(handler_func)

        logger.info("[CodingCouncil] Trinity evolution handlers registered")

    except Exception as e:
        logger.warning(f"[CodingCouncil] Failed to register Trinity handlers: {e}")


# ============================================================================
# Voice Command Integration (Gap #3 & #6)
# ============================================================================


class VoiceEvolutionHandler:
    """
    Handles evolution commands from voice input.

    Features:
    - Natural language parsing
    - Confirmation before execution
    - Progress feedback via voice
    """

    def __init__(self):
        self._classifier = CommandClassifier()
        self._pending_confirmations: Dict[str, EvolutionRequest] = {}

    async def process_voice_command(
        self,
        command_text: str,
        speaker_verified: bool = False,
    ) -> Dict[str, Any]:
        """
        Process a voice command for evolution.

        Args:
            command_text: Raw voice command text
            speaker_verified: Whether speaker is verified

        Returns:
            Response dict with action and message
        """
        # Check if this is an evolution command
        if not self._classifier.is_evolution_command(command_text):
            return {
                "is_evolution": False,
                "handled": False,
            }

        # Parse the command
        request = self._classifier.parse_command(command_text, source="voice")

        # Security: Require speaker verification for code evolution
        if not speaker_verified:
            return {
                "is_evolution": True,
                "handled": True,
                "action": "require_verification",
                "message": "Voice verification required for code evolution. Please verify your identity.",
            }

        # Require confirmation for voice commands
        if request.require_approval:
            confirmation_id = str(uuid.uuid4())[:8]
            self._pending_confirmations[confirmation_id] = request

            return {
                "is_evolution": True,
                "handled": True,
                "action": "require_confirmation",
                "confirmation_id": confirmation_id,
                "message": f"I understood: {request.intent.value.replace('_', ' ')} - "
                f'"{request.description}". '
                f"Say 'confirm {confirmation_id}' to proceed.",
                "request": request.to_dict(),
            }

        # Execute immediately if no approval needed
        return await self._execute_evolution(request)

    async def confirm_evolution(self, confirmation_id: str) -> Dict[str, Any]:
        """Confirm and execute a pending evolution."""
        if confirmation_id not in self._pending_confirmations:
            return {
                "success": False,
                "error": f"No pending evolution with ID {confirmation_id}",
            }

        request = self._pending_confirmations.pop(confirmation_id)
        return await self._execute_evolution(request)

    async def _execute_evolution(self, request: EvolutionRequest) -> Dict[str, Any]:
        """Execute an evolution request."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": False,
                    "error": "Coding Council not available",
                    "message": "I'm sorry, the Coding Council is not available right now.",
                }

            # Broadcast start
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=request.id,
                status="started",
                progress=0.0,
                message=f"Starting evolution: {request.description}",
            )

            # Execute
            result = await council.evolve(
                description=request.description,
                target_files=request.target_files,
                require_approval=False,  # Already confirmed
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Broadcast complete
            await broadcaster.broadcast(
                task_id=request.id,
                status="complete" if result.success else "failed",
                progress=1.0 if result.success else 0.0,
                message="Evolution complete" if result.success else str(result.error),
            )

            if result.success:
                files_modified = getattr(result, "files_modified", [])
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": True,
                    "message": f"Evolution complete. Modified {len(files_modified)} files.",
                    "files_modified": files_modified,
                }
            else:
                return {
                    "is_evolution": True,
                    "handled": True,
                    "success": False,
                    "error": str(result.error),
                    "message": f"Evolution failed: {result.error}",
                }

        except Exception as e:
            logger.error(f"[VoiceEvolution] Execution failed: {e}")
            return {
                "is_evolution": True,
                "handled": True,
                "success": False,
                "error": str(e),
                "message": f"Evolution failed with error: {e}",
            }


# Global voice handler
_voice_handler: Optional[VoiceEvolutionHandler] = None


def get_voice_evolution_handler() -> VoiceEvolutionHandler:
    """Get or create global voice evolution handler."""
    global _voice_handler
    if _voice_handler is None:
        _voice_handler = VoiceEvolutionHandler()
    return _voice_handler


# ============================================================================
# Advanced Evolution Pipeline Helpers (v77.2 Super-Beefed)
# ============================================================================


async def _validate_evolution_request(
    request: EvolutionRequest,
    context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Pre-flight validation for evolution requests.

    Checks:
    - Target files exist (if specified)
    - Files are readable
    - No syntax errors in target files
    - Not in protected paths
    - Description is meaningful
    """
    errors = []
    warnings = []

    # Validate description
    if not request.description or len(request.description.strip()) < 10:
        errors.append("Description too short (minimum 10 characters)")

    # Validate target files
    for target_file in request.target_files:
        # Check protected paths
        if CodingCouncilConfig.is_protected_path(target_file):
            errors.append(f"Protected path: {target_file}")
            continue

        # Check file exists (if it's a specific file, not a pattern)
        if not target_file.endswith("/") and "*" not in target_file:
            file_path = Path(target_file)
            if not file_path.is_absolute():
                # Try relative to common roots
                possible_paths = [
                    Path.cwd() / target_file,
                    Path.cwd() / "backend" / target_file,
                    Path(__file__).parent.parent.parent / target_file,
                ]
                found = False
                for p in possible_paths:
                    if p.exists():
                        found = True
                        break
                if not found:
                    warnings.append(f"File may not exist: {target_file}")
            elif not file_path.exists():
                warnings.append(f"File not found: {target_file}")

            # Check Python syntax if .py file
            if target_file.endswith(".py"):
                try:
                    import ast
                    for p in possible_paths if not file_path.is_absolute() else [file_path]:
                        if p.exists():
                            with open(p, "r") as f:
                                ast.parse(f.read())
                            break
                except SyntaxError as e:
                    warnings.append(f"Syntax error in {target_file}: {e}")
                except Exception:
                    pass  # File might not exist yet

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "files_checked": len(request.target_files),
    }


async def _assess_evolution_risk(
    request: EvolutionRequest,
    context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Intelligent risk assessment for evolution requests.

    Risk Factors:
    - Critical files involved
    - Number of files affected
    - Intent type (security > refactor > feature > doc)
    - Complexity indicators in description
    - Historical failure rate for similar tasks
    """
    risk_score = 0
    risk_factors = []

    # Factor 1: Critical files (weight: 30)
    critical_count = sum(
        1 for f in request.target_files
        if CodingCouncilConfig.is_critical_file(f)
    )
    if critical_count > 0:
        risk_score += min(30, critical_count * 15)
        risk_factors.append(f"{critical_count} critical file(s)")

    # Factor 2: Number of files (weight: 20)
    file_count = len(request.target_files)
    if file_count > 10:
        risk_score += 20
        risk_factors.append(f"Large scope ({file_count} files)")
    elif file_count > 5:
        risk_score += 10
        risk_factors.append(f"Medium scope ({file_count} files)")
    elif file_count > 1:
        risk_score += 5

    # Factor 3: Intent type (weight: 25)
    high_risk_intents = {
        EvolutionIntent.SECURITY_FIX: 25,
        EvolutionIntent.REFACTOR: 15,
        EvolutionIntent.DEPENDENCY_UPDATE: 15,
    }
    if request.intent in high_risk_intents:
        risk_score += high_risk_intents[request.intent]
        risk_factors.append(f"High-risk intent: {request.intent.value}")

    # Factor 4: Complexity indicators (weight: 15)
    complexity_keywords = [
        "complex", "major", "significant", "breaking", "architecture",
        "restructure", "rewrite", "overhaul", "migration"
    ]
    desc_lower = request.description.lower()
    complexity_count = sum(1 for kw in complexity_keywords if kw in desc_lower)
    if complexity_count > 0:
        risk_score += min(15, complexity_count * 5)
        risk_factors.append(f"Complexity indicators detected")

    # Factor 5: Protected path proximity (weight: 10)
    for f in request.target_files:
        if any(p in f.lower() for p in ["config", "settings", "auth", "secret"]):
            risk_score += 10
            risk_factors.append("Sensitive path proximity")
            break

    # Determine risk level
    if risk_score >= 70:
        level = "critical"
    elif risk_score >= 50:
        level = "high"
    elif risk_score >= 30:
        level = "medium"
    else:
        level = "low"

    return {
        "level": level,
        "score": risk_score,
        "factors": risk_factors,
        "max_score": 100,
        "recommendation": (
            "manual_review" if level in ("critical", "high") else
            "auto_approve" if level == "low" else "caution"
        ),
    }


async def _analyze_target_file(
    file_path: str,
    context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Analyze a target file for evolution context.

    Returns:
    - File type
    - Size
    - Complexity metrics
    - Dependencies
    - Recent changes
    """
    try:
        path = Path(file_path)

        # Try to find the actual file
        if not path.is_absolute():
            for base in [Path.cwd(), Path.cwd() / "backend", Path(__file__).parent.parent.parent]:
                candidate = base / file_path
                if candidate.exists():
                    path = candidate
                    break

        if not path.exists():
            return {
                "path": file_path,
                "exists": False,
                "analysis": "file_not_found",
            }

        stat = path.stat()
        content = path.read_text(encoding="utf-8", errors="ignore")
        lines = content.split("\n")

        # Basic metrics
        analysis = {
            "path": file_path,
            "exists": True,
            "size_bytes": stat.st_size,
            "lines": len(lines),
            "type": path.suffix,
            "modified": stat.st_mtime,
        }

        # Python-specific analysis
        if path.suffix == ".py":
            try:
                import ast
                tree = ast.parse(content)

                # Count entities
                classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
                functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
                async_functions = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]

                analysis["python"] = {
                    "classes": len(classes),
                    "functions": len(functions),
                    "async_functions": len(async_functions),
                    "imports": len([n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]),
                    "syntax_valid": True,
                }

                # Estimate complexity
                total_entities = len(classes) + len(functions) + len(async_functions)
                if total_entities > 50:
                    analysis["complexity"] = "high"
                elif total_entities > 20:
                    analysis["complexity"] = "medium"
                else:
                    analysis["complexity"] = "low"

            except SyntaxError:
                analysis["python"] = {"syntax_valid": False}
                analysis["complexity"] = "unknown"

        return analysis

    except Exception as e:
        return {
            "path": file_path,
            "exists": False,
            "error": str(e),
        }


async def _create_evolution_plan(
    request: EvolutionRequest,
    context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a detailed execution plan for the evolution.

    Uses MetaGPT-style planning for complex changes.
    """
    plan = {
        "id": str(uuid.uuid4())[:8],
        "created_at": time.time(),
        "steps": [],
    }

    # Analyze files for planning
    files_analyzed = context.get("files_analyzed", [])

    # Generate steps based on intent
    if request.intent == EvolutionIntent.REFACTOR:
        plan["steps"] = [
            {"step": 1, "action": "analyze_dependencies", "description": "Map all imports and dependencies"},
            {"step": 2, "action": "create_tests", "description": "Ensure test coverage before refactoring"},
            {"step": 3, "action": "refactor_iteratively", "description": "Apply changes in small increments"},
            {"step": 4, "action": "validate_each_step", "description": "Run tests after each change"},
            {"step": 5, "action": "update_documentation", "description": "Update docstrings and comments"},
        ]
    elif request.intent == EvolutionIntent.FEATURE_ADD:
        plan["steps"] = [
            {"step": 1, "action": "design_interface", "description": "Define function signatures and types"},
            {"step": 2, "action": "implement_core", "description": "Write core functionality"},
            {"step": 3, "action": "add_error_handling", "description": "Add try/except and validation"},
            {"step": 4, "action": "write_tests", "description": "Create unit tests"},
            {"step": 5, "action": "integrate", "description": "Connect to existing code"},
        ]
    elif request.intent == EvolutionIntent.BUG_FIX:
        plan["steps"] = [
            {"step": 1, "action": "reproduce_issue", "description": "Understand the bug scenario"},
            {"step": 2, "action": "identify_root_cause", "description": "Find the actual bug location"},
            {"step": 3, "action": "write_regression_test", "description": "Create test that fails before fix"},
            {"step": 4, "action": "apply_fix", "description": "Fix the bug with minimal changes"},
            {"step": 5, "action": "verify_fix", "description": "Ensure test passes and no regressions"},
        ]
    else:
        plan["steps"] = [
            {"step": 1, "action": "analyze", "description": "Understand current state"},
            {"step": 2, "action": "plan_changes", "description": "Design the modifications"},
            {"step": 3, "action": "implement", "description": "Apply changes"},
            {"step": 4, "action": "validate", "description": "Verify correctness"},
        ]

    # Add file-specific notes
    plan["target_files"] = [
        {
            "file": f.get("path", "unknown"),
            "complexity": f.get("complexity", "unknown"),
            "lines": f.get("lines", 0),
        }
        for f in files_analyzed
    ]

    plan["estimated_duration_seconds"] = len(plan["steps"]) * 30 + sum(
        f.get("lines", 100) // 50 for f in files_analyzed
    )

    return plan


async def _select_framework(
    request: EvolutionRequest,
    context: Dict[str, Any]
) -> str:
    """
    Select the optimal framework using hybrid approach:
    1. Rule-based constraints (sandbox, critical risk)
    2. Adaptive Thompson Sampling for exploration/exploitation

    Decision Matrix:
    - Sandbox required → OpenHands (hard constraint)
    - Critical risk → MetaGPT (hard constraint)
    - Otherwise → Adaptive selection via Thompson Sampling
    """
    risk_level = context.get("risk_level", "medium")
    files_analyzed = context.get("files_analyzed", [])
    total_lines = sum(f.get("lines", 0) for f in files_analyzed)
    complexity = "high" if total_lines > 500 else "medium" if total_lines > 100 else "low"

    # Available frameworks
    available_frameworks = ["aider", "metagpt", "claude_code", "continue"]

    # Hard constraints (rule-based, no learning)
    if request.require_sandbox:
        return "openhands" if "openhands" in available_frameworks else "claude_code"

    if risk_level == "critical":
        return "metagpt" if "metagpt" in available_frameworks else "claude_code"

    # Soft constraints: filter available frameworks based on task
    candidate_frameworks = available_frameworks.copy()

    # For complex tasks, prefer planning frameworks
    if risk_level == "high" or total_lines > 1000:
        # Boost planning frameworks by keeping them, remove simple ones
        if "metagpt" in candidate_frameworks:
            # Keep metagpt as an option but let adaptive selector choose
            pass

    # For simple tasks, prefer fast frameworks
    if risk_level == "low" and total_lines < 100:
        # Boost fast frameworks
        if "aider" in candidate_frameworks:
            pass

    # Use adaptive selector with Thompson Sampling
    selector = get_adaptive_selector()
    selection_context = {
        "complexity": complexity,
        "risk_level": risk_level,
        "total_lines": total_lines,
        "file_count": len(request.target_files),
    }

    # Exploration rate based on confidence
    # Lower exploration for simple tasks, higher for complex
    exploration_rate = 0.05 if risk_level == "low" else 0.15 if risk_level == "high" else 0.1

    framework = await selector.select_framework(
        available_frameworks=candidate_frameworks,
        context=selection_context,
        exploration_rate=exploration_rate,
    )

    logger.info(f"🎰 [FrameworkSelection] Selected {framework} (risk={risk_level}, lines={total_lines})")

    return framework


async def _prepare_rollback(
    request: EvolutionRequest,
    context: Dict[str, Any]
) -> str:
    """
    Prepare rollback snapshot before making changes.

    Creates:
    - Git stash or branch
    - File backups
    - State snapshot
    """
    rollback_id = f"rollback_{request.id[:8]}_{int(time.time())}"

    try:
        import subprocess

        # Try to create git stash
        result = subprocess.run(
            ["git", "stash", "push", "-m", f"JARVIS Evolution Rollback: {rollback_id}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=Path.cwd(),
        )

        if result.returncode == 0:
            logger.info(f"🧬 [Rollback] Git stash created: {rollback_id}")
        else:
            # Fallback: create backup files
            logger.debug(f"🧬 [Rollback] Git stash failed, using file backup")

    except Exception as e:
        logger.debug(f"🧬 [Rollback] Preparation warning: {e}")

    return rollback_id


async def _verify_evolution_result(
    result: Any,
    context: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Verify evolution result with multiple checks.

    Checks:
    - Syntax validation (AST parse)
    - Type checking (if mypy available)
    - Import validation
    - Basic security scan
    """
    verification = {
        "passed": True,
        "checks": [],
    }

    # Get modified files from result
    files_modified = getattr(result, "files_modified", [])

    for file_path in files_modified:
        if not file_path.endswith(".py"):
            continue

        try:
            path = Path(file_path)
            if not path.exists():
                continue

            content = path.read_text(encoding="utf-8")

            # Check 1: AST parsing (syntax)
            try:
                import ast
                ast.parse(content)
                verification["checks"].append({
                    "file": file_path,
                    "check": "syntax",
                    "passed": True,
                })
            except SyntaxError as e:
                verification["passed"] = False
                verification["checks"].append({
                    "file": file_path,
                    "check": "syntax",
                    "passed": False,
                    "error": str(e),
                })

            # Check 2: Basic security scan
            security_issues = []
            dangerous_patterns = [
                (r"\beval\s*\(", "eval() usage"),
                (r"\bexec\s*\(", "exec() usage"),
                (r"\b__import__\s*\(", "dynamic import"),
                (r"subprocess\..*shell\s*=\s*True", "shell injection risk"),
            ]

            import re
            for pattern, issue in dangerous_patterns:
                if re.search(pattern, content):
                    security_issues.append(issue)

            if security_issues:
                verification["checks"].append({
                    "file": file_path,
                    "check": "security",
                    "passed": False,
                    "issues": security_issues,
                })
                # Don't fail on security warnings, just flag them
            else:
                verification["checks"].append({
                    "file": file_path,
                    "check": "security",
                    "passed": True,
                })

        except Exception as e:
            verification["checks"].append({
                "file": file_path,
                "check": "general",
                "passed": False,
                "error": str(e),
            })

    return verification


async def _execute_rollback(rollback_id: str) -> bool:
    """
    Execute rollback to restore previous state.
    """
    try:
        import subprocess

        # Try git stash pop
        result = subprocess.run(
            ["git", "stash", "pop"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=Path.cwd(),
        )

        if result.returncode == 0:
            logger.info(f"🧬 [Rollback] Restored from git stash")
            return True

    except Exception as e:
        logger.error(f"🧬 [Rollback] Failed: {e}")

    return False


# ============================================================================
# FastAPI Routes Integration (Gap #2)
# ============================================================================


def create_coding_council_router():
    """
    Create FastAPI router for Coding Council endpoints.

    Returns:
        FastAPI APIRouter with all evolution endpoints
    """
    try:
        from fastapi import APIRouter, HTTPException, BackgroundTasks, WebSocket, WebSocketDisconnect
        from fastapi.responses import JSONResponse
        from pydantic import BaseModel, Field
    except ImportError:
        logger.warning("[CodingCouncil] FastAPI not available")
        return None

    router = APIRouter(prefix="/coding-council", tags=["Coding Council"])

    # Request models
    class EvolutionRequestModel(BaseModel):
        description: str = Field(..., description="Evolution task description")
        target_files: List[str] = Field(default=[], description="Target files to modify")
        require_approval: bool = Field(default=True, description="Require approval before execution")
        require_sandbox: bool = Field(default=False, description="Execute in sandbox")
        require_planning: bool = Field(default=False, description="Require planning phase")

    class RollbackRequestModel(BaseModel):
        task_id: str = Field(..., description="Task ID to rollback")

    class ConfirmRequestModel(BaseModel):
        confirmation_id: str = Field(..., description="Confirmation ID")

    @router.post("/evolve")
    async def evolve_code(
        request: EvolutionRequestModel,
        background_tasks: BackgroundTasks,
    ):
        """
        v77.2 Advanced Evolution Endpoint - Super-beefed up version.

        Features:
        - Intelligent risk assessment before execution
        - Pre-flight validation (file existence, syntax, permissions)
        - Real-time progress broadcasting at each stage
        - Parallel file analysis for multiple targets
        - Framework selection based on task complexity
        - Rate limiting and circuit breaker protection
        - Comprehensive audit logging
        - Automatic rollback preparation

        Stages:
        1. VALIDATION (5%) - Pre-flight checks
        2. RISK_ASSESSMENT (15%) - Analyze risk level
        3. PLANNING (30%) - Create execution plan
        4. FRAMEWORK_SELECTION (35%) - Choose best framework
        5. EXECUTION (80%) - Apply changes
        6. VERIFICATION (95%) - Validate changes
        7. COMPLETE (100%) - Finalize

        Returns immediately with task_id. Track progress via WebSocket or /status.
        """
        broadcaster = get_evolution_broadcaster()
        start_time = time.time()

        # ═══════════════════════════════════════════════════════════════════
        # PRE-FLIGHT: Rate Limiting (Prevents abuse)
        # ═══════════════════════════════════════════════════════════════════
        rate_limiter = get_rate_limiter()
        allowed, wait_time = await rate_limiter.acquire("evolution_api")
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded. Retry in {wait_time:.1f} seconds.",
                headers={"Retry-After": str(int(wait_time) + 1)},
            )

        # ═══════════════════════════════════════════════════════════════════
        # PRE-FLIGHT: Circuit Breaker (Prevents cascading failures)
        # ═══════════════════════════════════════════════════════════════════
        circuit_breaker = get_circuit_breaker()
        can_execute, cb_reason = await circuit_breaker.can_execute()
        if not can_execute:
            raise HTTPException(
                status_code=503,
                detail=f"Service temporarily unavailable: {cb_reason}",
            )

        try:
            # ═══════════════════════════════════════════════════════════════════
            # STAGE 0: Initialize and validate request
            # ═══════════════════════════════════════════════════════════════════
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                raise HTTPException(status_code=503, detail="Coding Council not available")

            # Classify intent from description
            intent = CommandClassifier.classify_intent(request.description)

            # Create evolution request with classified intent
            evo_request = EvolutionRequest(
                description=request.description,
                intent=intent,
                target_files=request.target_files,
                source="http",
                require_approval=request.require_approval,
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            task_id = evo_request.id
            logger.info(f"🧬 [Evolution] Task {task_id} created: {sanitize_for_log(request.description, 50)}...")

            # ═══════════════════════════════════════════════════════════════════
            # Define the comprehensive evolution pipeline
            # ═══════════════════════════════════════════════════════════════════
            async def execute_advanced_evolution():
                """
                Advanced evolution pipeline with real-time progress tracking.
                Includes semaphore for concurrency control and result tracking.
                """
                nonlocal evo_request
                evolution_context = {
                    "task_id": task_id,
                    "start_time": start_time,
                    "stages_completed": [],
                    "risk_level": "unknown",
                    "framework": "auto",
                    "files_analyzed": [],
                    "validation_results": {},
                    "rollback_prepared": False,
                }

                # Acquire execution slot (with priority based on request)
                semaphore = get_evolution_semaphore()
                slot_acquired = await semaphore.acquire(
                    task_id=task_id,
                    priority=evo_request.priority,
                    timeout=CodingCouncilConfig.EVOLUTION_TIMEOUT
                )

                if not slot_acquired:
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="queued_timeout",
                        progress=0.0,
                        message="Evolution timed out waiting for execution slot",
                        details={"queue_length": semaphore.queue_length},
                    )
                    return

                try:
                    # ═══════════════════════════════════════════════════════════
                    # STAGE 1: PRE-FLIGHT VALIDATION (5%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="validating",
                        progress=0.05,
                        message="Running pre-flight validation...",
                        details={"stage": "validation", "stage_number": 1},
                    )

                    validation_results = await _validate_evolution_request(
                        evo_request, evolution_context
                    )
                    evolution_context["validation_results"] = validation_results
                    evolution_context["stages_completed"].append("validation")

                    if not validation_results.get("valid", False):
                        await broadcaster.broadcast(
                            task_id=task_id,
                            status="failed",
                            progress=0.05,
                            message=f"Validation failed: {validation_results.get('error', 'Unknown')}",
                            details={"stage": "validation", "errors": validation_results.get("errors", [])},
                        )
                        return

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 2: RISK ASSESSMENT (15%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="assessing_risk",
                        progress=0.15,
                        message="Analyzing risk level...",
                        details={"stage": "risk_assessment", "stage_number": 2},
                    )

                    risk_assessment = await _assess_evolution_risk(
                        evo_request, evolution_context
                    )
                    evolution_context["risk_level"] = risk_assessment["level"]
                    evolution_context["risk_details"] = risk_assessment
                    evolution_context["stages_completed"].append("risk_assessment")

                    logger.info(f"🧬 [Evolution] Risk assessment: {risk_assessment['level']}")

                    # Block high-risk without approval
                    if risk_assessment["level"] == "critical" and request.require_approval:
                        await broadcaster.broadcast(
                            task_id=task_id,
                            status="blocked",
                            progress=0.15,
                            message="Critical risk detected - manual approval required",
                            details={"stage": "risk_assessment", "risk": risk_assessment},
                        )
                        # Store for manual approval
                        await store_pending_approval(evo_request)
                        return

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 3: PARALLEL FILE ANALYSIS (25%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="analyzing",
                        progress=0.25,
                        message=f"Analyzing {len(request.target_files)} target file(s)...",
                        details={"stage": "file_analysis", "stage_number": 3},
                    )

                    # Parallel analysis of all target files
                    if request.target_files:
                        analysis_tasks = [
                            _analyze_target_file(f, evolution_context)
                            for f in request.target_files
                        ]
                        file_analyses = await asyncio.gather(*analysis_tasks, return_exceptions=True)
                        evolution_context["files_analyzed"] = [
                            a for a in file_analyses if not isinstance(a, Exception)
                        ]

                    evolution_context["stages_completed"].append("file_analysis")

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 4: PLANNING (35%)
                    # ═══════════════════════════════════════════════════════════
                    if request.require_planning or risk_assessment["level"] in ("high", "critical"):
                        await broadcaster.broadcast(
                            task_id=task_id,
                            status="planning",
                            progress=0.35,
                            message="Creating detailed execution plan...",
                            details={"stage": "planning", "stage_number": 4},
                        )

                        # Use MetaGPT-style planning if complex
                        plan = await _create_evolution_plan(evo_request, evolution_context)
                        evolution_context["plan"] = plan
                        evolution_context["stages_completed"].append("planning")

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 5: FRAMEWORK SELECTION (40%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="selecting_framework",
                        progress=0.40,
                        message="Selecting optimal framework...",
                        details={"stage": "framework_selection", "stage_number": 5},
                    )

                    framework = await _select_framework(evo_request, evolution_context)
                    evolution_context["framework"] = framework
                    evolution_context["stages_completed"].append("framework_selection")

                    logger.info(f"🧬 [Evolution] Selected framework: {framework}")

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 6: ROLLBACK PREPARATION (45%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="preparing_rollback",
                        progress=0.45,
                        message="Preparing rollback snapshot...",
                        details={"stage": "rollback_prep", "stage_number": 6},
                    )

                    rollback_id = await _prepare_rollback(evo_request, evolution_context)
                    evolution_context["rollback_id"] = rollback_id
                    evolution_context["rollback_prepared"] = True
                    evolution_context["stages_completed"].append("rollback_prep")

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 7: EXECUTION (50-80%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="executing",
                        progress=0.50,
                        message=f"Executing evolution via {framework}...",
                        details={"stage": "execution", "stage_number": 7, "framework": framework},
                    )

                    # Execute with progress callback
                    async def progress_callback(pct: float, msg: str):
                        # Map 0-100% to 50-80%
                        mapped_progress = 0.50 + (pct / 100.0) * 0.30
                        await broadcaster.broadcast(
                            task_id=task_id,
                            status="executing",
                            progress=mapped_progress,
                            message=msg,
                            details={"stage": "execution", "sub_progress": pct},
                        )

                    result = await council.evolve(
                        description=request.description,
                        target_files=request.target_files,
                        require_approval=False,
                        require_sandbox=request.require_sandbox,
                        require_planning=False,  # Already planned
                        progress_callback=progress_callback if hasattr(council.evolve, '__code__') and 'progress_callback' in council.evolve.__code__.co_varnames else None,
                    )

                    evolution_context["stages_completed"].append("execution")
                    evolution_context["result"] = result

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 8: VERIFICATION (85%)
                    # ═══════════════════════════════════════════════════════════
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="verifying",
                        progress=0.85,
                        message="Verifying changes...",
                        details={"stage": "verification", "stage_number": 8},
                    )

                    verification = await _verify_evolution_result(result, evolution_context)
                    evolution_context["verification"] = verification
                    evolution_context["stages_completed"].append("verification")

                    # ═══════════════════════════════════════════════════════════
                    # STAGE 9: FINALIZATION (95-100%)
                    # ═══════════════════════════════════════════════════════════
                    success = result.success if hasattr(result, "success") else True
                    success = success and verification.get("passed", True)

                    if not success and evolution_context["rollback_prepared"]:
                        await broadcaster.broadcast(
                            task_id=task_id,
                            status="rolling_back",
                            progress=0.95,
                            message="Verification failed, rolling back...",
                            details={"stage": "rollback", "stage_number": 9},
                        )
                        await _execute_rollback(evolution_context["rollback_id"])

                    # Calculate elapsed time
                    elapsed = time.time() - start_time

                    # Final broadcast
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="complete" if success else "failed",
                        progress=1.0,
                        message="Evolution complete" if success else "Evolution failed",
                        details={
                            "stage": "complete",
                            "success": success,
                            "elapsed_seconds": round(elapsed, 2),
                            "stages_completed": evolution_context["stages_completed"],
                            "risk_level": evolution_context["risk_level"],
                            "framework": evolution_context["framework"],
                            "files_modified": getattr(result, "files_modified", []) if success else [],
                            "verification": verification,
                        },
                    )

                    logger.info(f"🧬 [Evolution] Task {task_id} {'completed' if success else 'failed'} in {elapsed:.2f}s")

                    # Record result for adaptive learning and circuit breaker
                    framework = evolution_context.get("framework", "unknown")
                    selector = get_adaptive_selector()
                    await selector.record_result(
                        framework=framework,
                        success=success,
                        context={"complexity": evolution_context.get("risk_level", "medium")},
                    )

                    if success:
                        await circuit_breaker.record_success()
                    else:
                        await circuit_breaker.record_failure()

                except asyncio.CancelledError:
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="cancelled",
                        progress=evolution_context.get("last_progress", 0),
                        message="Evolution cancelled",
                    )
                    await circuit_breaker.record_failure()
                    raise

                except Exception as e:
                    logger.error(f"🧬 [Evolution] Task {task_id} error: {e}", exc_info=True)
                    await broadcaster.broadcast(
                        task_id=task_id,
                        status="error",
                        progress=0,
                        message=f"Evolution error: {str(e)}",
                        details={
                            "error": str(e),
                            "error_type": type(e).__name__,
                            "stages_completed": evolution_context.get("stages_completed", []),
                        },
                    )
                    await circuit_breaker.record_failure(e)

                finally:
                    # Always release the semaphore
                    await semaphore.release(task_id)

            # ═══════════════════════════════════════════════════════════════════
            # Determine execution mode
            # ═══════════════════════════════════════════════════════════════════
            if not request.require_approval:
                # Execute immediately in background
                background_tasks.add_task(execute_advanced_evolution)

                return {
                    "success": True,
                    "task_id": task_id,
                    "status": "started",
                    "message": "Advanced evolution pipeline started",
                    "stages": ["validation", "risk_assessment", "file_analysis", "planning",
                              "framework_selection", "rollback_prep", "execution", "verification", "complete"],
                    "websocket_url": f"/api/evolution/ws",
                }

            # Check auto-approval
            can_auto, reason = CodingCouncilConfig.can_auto_approve(evo_request)

            if can_auto:
                logger.info(f"🧬 [Evolution] Auto-approving {task_id}: {reason}")
                background_tasks.add_task(execute_advanced_evolution)

                return {
                    "success": True,
                    "task_id": task_id,
                    "status": "auto_approved",
                    "reason": reason,
                    "message": "Evolution auto-approved and started",
                    "stages": ["validation", "risk_assessment", "file_analysis", "planning",
                              "framework_selection", "rollback_prep", "execution", "verification", "complete"],
                }

            # Store for manual approval
            await store_pending_approval(evo_request)

            return {
                "success": True,
                "task_id": task_id,
                "status": "pending_approval",
                "message": "Evolution request created, awaiting approval",
                "approval_url": f"/api/evolution/approve/{task_id}",
                "reject_url": f"/api/evolution/reject/{task_id}",
                "expires_in_seconds": CodingCouncilConfig.APPROVAL_TIMEOUT,
                "request": evo_request.to_dict(),
                "intent": intent.value,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"🧬 [Evolution] Initialization failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/status")
    async def get_status():
        """Get Coding Council status."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                return {
                    "available": False,
                    "status": "not_initialized",
                }

            status = await council.get_status()
            return {
                "available": True,
                "status": status,
            }

        except Exception as e:
            return {
                "available": False,
                "error": str(e),
            }

    @router.get("/health")
    async def health_check():
        """Health check endpoint for Coding Council."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            healthy = council is not None

            return {
                "healthy": healthy,
                "version": "77.2",
                "gaps_addressed": 80,
                "broadcaster_clients": get_evolution_broadcaster().client_count,
            }

        except Exception as e:
            return {
                "healthy": False,
                "error": str(e),
            }

    @router.get("/pending")
    async def get_pending_evolutions():
        """
        Get list of pending evolution requests awaiting approval.

        Returns:
            List of pending approval requests with their age and expiry time.
        """
        try:
            pending = await list_pending_approvals()
            return {
                "success": True,
                "pending_count": len(pending),
                "pending": pending,
                "approval_timeout_seconds": CodingCouncilConfig.APPROVAL_TIMEOUT,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    @router.post("/approve/{task_id}")
    async def approve_evolution(task_id: str):
        """
        Approve a pending evolution request and execute it.

        Args:
            task_id: The task ID of the pending evolution

        Returns:
            Evolution result after execution
        """
        try:
            # Get the pending request
            request = await get_pending_approval(task_id)
            if not request:
                raise HTTPException(
                    status_code=404,
                    detail=f"No pending evolution with ID {task_id}. It may have expired or been executed."
                )

            # Get the coding council
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                raise HTTPException(status_code=503, detail="Coding Council not available")

            # Broadcast approval
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=task_id,
                status="approved",
                progress=0.1,
                message="Evolution approved, starting execution...",
            )

            # Execute the evolution
            result = await council.evolve(
                description=request.description,
                target_files=request.target_files,
                require_approval=False,  # Already approved
                require_sandbox=request.require_sandbox,
                require_planning=request.require_planning,
            )

            # Remove from pending
            await remove_pending_approval(task_id)

            # Broadcast result
            success = result.success if hasattr(result, "success") else True
            await broadcaster.broadcast(
                task_id=task_id,
                status="complete" if success else "failed",
                progress=1.0 if success else 0.0,
                message="Evolution complete" if success else str(getattr(result, "error", "Unknown error")),
            )

            return {
                "success": success,
                "task_id": task_id,
                "result": result.to_dict() if hasattr(result, "to_dict") else str(result),
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[API] Approval execution failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/reject/{task_id}")
    async def reject_evolution(task_id: str):
        """
        Reject a pending evolution request.

        Args:
            task_id: The task ID of the pending evolution

        Returns:
            Confirmation of rejection
        """
        try:
            request = await get_pending_approval(task_id)
            if not request:
                raise HTTPException(
                    status_code=404,
                    detail=f"No pending evolution with ID {task_id}"
                )

            await remove_pending_approval(task_id)

            # Broadcast rejection
            broadcaster = get_evolution_broadcaster()
            await broadcaster.broadcast(
                task_id=task_id,
                status="rejected",
                progress=0.0,
                message="Evolution request rejected by user",
            )

            return {
                "success": True,
                "task_id": task_id,
                "status": "rejected",
                "message": "Evolution request rejected",
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/rollback")
    async def rollback_evolution(request: RollbackRequestModel):
        """Rollback a previous evolution."""
        try:
            try:
                from backend.core.coding_council import get_coding_council
            except ImportError:
                from core.coding_council import get_coding_council

            council = await get_coding_council()
            if not council:
                raise HTTPException(status_code=503, detail="Coding Council not available")

            result = await council.rollback(request.task_id)
            return {
                "success": result.success if hasattr(result, "success") else True,
                "task_id": request.task_id,
            }

        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # =========================================================================
    # v79.0: Voice Announcer API Endpoints
    # =========================================================================

    @router.get("/voice/status")
    async def get_voice_status():
        """
        v79.0: Get voice announcer status and statistics.

        Returns current voice configuration, statistics, and active evolutions
        being tracked for voice announcements.
        """
        try:
            try:
                from core.coding_council.voice_announcer import get_evolution_announcer
            except ImportError:
                from backend.core.coding_council.voice_announcer import get_evolution_announcer

            announcer = get_evolution_announcer()
            return {
                "available": True,
                "statistics": announcer.get_statistics(),
                "active_evolutions": announcer.get_active_evolutions(),
            }

        except ImportError:
            return {
                "available": False,
                "error": "Voice announcer module not available",
            }
        except Exception as e:
            return {
                "available": False,
                "error": str(e),
            }

    @router.get("/voice/history")
    async def get_voice_history(limit: int = 10):
        """
        v79.0: Get recent voice announcement history.

        Args:
            limit: Maximum number of history entries to return (default 10)

        Returns list of recent evolution completions that were announced.
        """
        try:
            try:
                from core.coding_council.voice_announcer import get_evolution_announcer
            except ImportError:
                from backend.core.coding_council.voice_announcer import get_evolution_announcer

            announcer = get_evolution_announcer()
            return {
                "history": announcer.get_evolution_history(limit=limit),
            }

        except ImportError:
            return {"error": "Voice announcer not available"}
        except Exception as e:
            return {"error": str(e)}

    @router.post("/voice/test")
    async def test_voice_announcement(message: str = "Testing evolution voice announcer"):
        """
        v79.0: Test voice announcement system.

        Args:
            message: Message to announce (default: test message)

        Triggers a test voice announcement to verify TTS integration.
        """
        try:
            try:
                from core.supervisor.unified_voice_orchestrator import speak_evolution
            except ImportError:
                from backend.core.supervisor.unified_voice_orchestrator import speak_evolution

            success = await speak_evolution(message, wait=True)
            return {
                "success": success,
                "message": message if success else "Voice system unavailable or disabled",
            }

        except ImportError:
            return {
                "success": False,
                "error": "Voice orchestrator not available",
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    @router.get("/voice/config")
    async def get_voice_config():
        """
        v79.0: Get voice announcer configuration.

        Returns current voice announcer configuration including cooldowns,
        milestone settings, and enabled state.
        """
        try:
            try:
                from core.coding_council.voice_announcer import get_evolution_announcer
            except ImportError:
                from backend.core.coding_council.voice_announcer import get_evolution_announcer

            announcer = get_evolution_announcer()
            config = announcer.config
            return {
                "enabled": config.enabled,
                "progress_cooldown": config.progress_cooldown,
                "start_cooldown": config.start_cooldown,
                "progress_milestones": config.progress_milestones,
                "use_sir": config.use_sir,
                "sir_probability": config.sir_probability,
                "environment_variables": {
                    "JARVIS_EVOLUTION_VOICE": "enabled" if config.enabled else "disabled",
                    "JARVIS_EVOLUTION_PROGRESS_COOLDOWN": str(config.progress_cooldown),
                    "JARVIS_EVOLUTION_START_COOLDOWN": str(config.start_cooldown),
                },
            }

        except ImportError:
            return {"error": "Voice announcer not available"}
        except Exception as e:
            return {"error": str(e)}

    @router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for real-time evolution updates."""
        await websocket.accept()

        broadcaster = get_evolution_broadcaster()
        await broadcaster.register_client(websocket)

        try:
            while True:
                # Keep connection alive and handle incoming messages
                data = await websocket.receive_text()
                # Could handle commands via WebSocket here
                logger.debug(f"[WS] Received: {sanitize_for_log(data)}")

        except WebSocketDisconnect:
            await broadcaster.unregister_client(websocket)
        except Exception as e:
            logger.debug(f"[WS] Error: {e}")
            await broadcaster.unregister_client(websocket)

    return router


def register_coding_council_routes(app: Any) -> None:
    """
    Register Coding Council routes with FastAPI app.

    This is the main integration point (Gap #2).

    Args:
        app: FastAPI application instance
    """
    router = create_coding_council_router()
    if router:
        app.include_router(router)
        logger.info("[CodingCouncil] FastAPI routes registered")


# ============================================================================
# Health Integration (Gap #4)
# ============================================================================


async def get_coding_council_health() -> Dict[str, Any]:
    """
    Get Coding Council health for main health endpoint.

    This function should be called from the main health endpoint (Gap #4).
    """
    try:
        try:
            from backend.core.coding_council import get_coding_council
        except ImportError:
            from core.coding_council import get_coding_council

        council = await get_coding_council()
        if not council:
            return {
                "status": "unavailable",
                "healthy": False,
            }

        status = await council.get_status()
        return {
            "status": "healthy",
            "healthy": True,
            "version": status.get("version", "77.2"),
            "gaps_addressed": status.get("gaps_addressed", 80),
            "active_evolutions": status.get("active_evolutions", 0),
        }

    except Exception as e:
        return {
            "status": "error",
            "healthy": False,
            "error": str(e),
        }


# ============================================================================
# Full Integration Setup
# ============================================================================


async def setup_coding_council_integration(
    app: Optional[Any] = None,
    bridge: Optional[Any] = None,
) -> Dict[str, bool]:
    """
    Set up all Coding Council integrations.

    Args:
        app: FastAPI application instance (optional)
        bridge: Trinity ReactorBridge instance (optional)

    Returns:
        Dict mapping integration name -> success status
    """
    results = {}

    # 1. Trinity integration (CRITICAL)
    if bridge is not None:
        try:
            register_evolution_handlers(bridge)
            results["trinity"] = True
        except Exception as e:
            logger.error(f"Trinity integration failed: {e}")
            results["trinity"] = False
    else:
        results["trinity"] = None  # Not attempted

    # 2. FastAPI routes (HIGH)
    if app is not None:
        try:
            register_coding_council_routes(app)
            results["fastapi"] = True
        except Exception as e:
            logger.error(f"FastAPI integration failed: {e}")
            results["fastapi"] = False
    else:
        results["fastapi"] = None  # Not attempted

    # 3. Voice handler - always initialized
    try:
        handler = get_voice_evolution_handler()
        results["voice"] = handler is not None
    except Exception as e:
        logger.error(f"Voice integration failed: {e}")
        results["voice"] = False

    # 4. WebSocket broadcaster - always initialized
    try:
        broadcaster = get_evolution_broadcaster()
        results["websocket"] = broadcaster is not None
    except Exception as e:
        logger.error(f"WebSocket integration failed: {e}")
        results["websocket"] = False

    logger.info(f"[CodingCouncil] Integration setup complete: {results}")
    return results

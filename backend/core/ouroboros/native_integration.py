"""
Native Self-Improvement Integration v2.0
========================================

This module transforms Ouroboros from an external CLI into a native capability
of the JARVIS Body. Like how your hand is part of your body - you don't "exit"
yourself to use your hand, you simply think and your hand moves.

v2.0 Enhancements:
    - Integrates with Trinity Integration Layer v2.0
    - Distributed locking for concurrent improvements
    - Code review before applying changes (Coding Council)
    - Automatic rollback on failure
    - Learning cache to avoid repeated failures
    - Circular dependency detection
    - Manual review queue for complete failures

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                    JARVIS BODY (The Living System)                       │
    ├─────────────────────────────────────────────────────────────────────────┤
    │                                                                          │
    │  ┌─────────────────────────────────────────────────────────────────┐    │
    │  │                    CONSCIOUS MIND (JARVIS Prime)                 │    │
    │  │  "I realize there's a bug in main.py"                           │    │
    │  └─────────────────────┬───────────────────────────────────────────┘    │
    │                        │                                                 │
    │                        ▼ (Intent)                                        │
    │  ┌─────────────────────────────────────────────────────────────────┐    │
    │  │               NATIVE SELF-IMPROVEMENT (Motor Function)           │    │
    │  │                                                                  │    │
    │  │  execute_self_improvement(                                       │    │
    │  │      target="main.py",                                           │    │
    │  │      goal="Fix the race condition bug"                           │    │
    │  │  )                                                               │    │
    │  │                                                                  │    │
    │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐         │    │
    │  │  │ Analyze  │─▶│ Generate │─▶│ Validate │─▶│  Apply   │         │    │
    │  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘         │    │
    │  │       │              │             │             │              │    │
    │  │       ▼              ▼             ▼             ▼              │    │
    │  │    ┌──────────────────────────────────────────────────┐         │    │
    │  │    │     PROGRESS BROADCASTER (To Menu Bar & UI)       │         │    │
    │  │    │  "Analyzing... Generating... Testing... Done!"    │         │    │
    │  │    └──────────────────────────────────────────────────┘         │    │
    │  └─────────────────────────────────────────────────────────────────┘    │
    │                                                                          │
    │                        ▼ (Feedback)                                      │
    │  ┌─────────────────────────────────────────────────────────────────┐    │
    │  │                    NERVOUS SYSTEM (Reactor Core)                 │    │
    │  │  "I've learned from this improvement - storing experience"       │    │
    │  └─────────────────────────────────────────────────────────────────┘    │
    │                                                                          │
    └─────────────────────────────────────────────────────────────────────────┘

Security Features:
    - Path traversal protection with canonical path validation
    - Command injection prevention with parameterized execution
    - Thread-safe metrics with atomic operations
    - Circuit breaker with proper state machine
    - Rate limiting with burst protection
    - Sandbox environment isolation

Author: Trinity System
Version: 2.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import time
import uuid
import weakref
from datetime import datetime
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from functools import wraps
from pathlib import Path
from threading import Lock as ThreadLock
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

try:
    import aiofiles
except ImportError:
    aiofiles = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

logger = logging.getLogger("Ouroboros.NativeIntegration")

T = TypeVar("T")


# =============================================================================
# CONFIGURATION - Dynamic, No Hardcoding
# =============================================================================

class NativeConfig:
    """
    Dynamic configuration with validation.
    All values from environment with sensible defaults and range validation.
    """

    # Project paths - dynamically resolved
    PROJECT_ROOT = Path(os.getenv(
        "JARVIS_PROJECT_ROOT",
        Path(__file__).parent.parent.parent.parent
    ))

    # Allowed paths for self-improvement (security boundary)
    ALLOWED_PATHS: List[Path] = [
        PROJECT_ROOT,
        Path(os.getenv("JARVIS_PRIME_ROOT", Path.home() / "Documents/repos/JARVIS-Prime")),
        Path(os.getenv("REACTOR_CORE_ROOT", Path.home() / "Documents/repos/reactor-core")),
    ]

    # Rate limiting (validated)
    @staticmethod
    def get_rate_limit() -> float:
        value = float(os.getenv("OUROBOROS_RATE_LIMIT", "1.0"))
        return max(0.01, min(100.0, value))  # Clamp to valid range

    @staticmethod
    def get_burst_limit() -> int:
        value = int(os.getenv("OUROBOROS_BURST_LIMIT", "5"))
        return max(1, min(50, value))

    # Timeouts (validated)
    @staticmethod
    def get_llm_timeout() -> float:
        value = float(os.getenv("OUROBOROS_LLM_TIMEOUT", "120.0"))
        return max(10.0, min(600.0, value))

    @staticmethod
    def get_test_timeout() -> float:
        value = float(os.getenv("OUROBOROS_TEST_TIMEOUT", "300.0"))
        return max(10.0, min(1800.0, value))

    @staticmethod
    def get_lock_timeout() -> float:
        value = float(os.getenv("OUROBOROS_LOCK_TIMEOUT", "30.0"))
        return max(1.0, min(300.0, value))

    # Resource limits (validated)
    @staticmethod
    def get_max_file_size() -> int:
        """Maximum file size in bytes (default 1MB)."""
        value = int(os.getenv("OUROBOROS_MAX_FILE_SIZE", str(1024 * 1024)))
        return max(1024, min(100 * 1024 * 1024, value))

    @staticmethod
    def get_max_output_size() -> int:
        """Maximum output size in bytes (default 100KB)."""
        value = int(os.getenv("OUROBOROS_MAX_OUTPUT_SIZE", str(100 * 1024)))
        return max(1024, min(10 * 1024 * 1024, value))

    # Retry configuration (validated)
    @staticmethod
    def get_max_retries() -> int:
        value = int(os.getenv("OUROBOROS_MAX_RETRIES", "5"))
        return max(1, min(20, value))

    @staticmethod
    def get_retry_backoff_base() -> float:
        value = float(os.getenv("OUROBOROS_RETRY_BACKOFF", "2.0"))
        return max(1.1, min(5.0, value))

    # Circuit breaker (validated)
    @staticmethod
    def get_circuit_threshold() -> int:
        value = int(os.getenv("OUROBOROS_CIRCUIT_THRESHOLD", "5"))
        return max(1, min(100, value))

    @staticmethod
    def get_circuit_timeout() -> float:
        value = float(os.getenv("OUROBOROS_CIRCUIT_TIMEOUT", "300.0"))
        return max(10.0, min(3600.0, value))

    @staticmethod
    def get_circuit_recovery_threshold() -> int:
        """Consecutive successes needed to close circuit from HALF_OPEN."""
        value = int(os.getenv("OUROBOROS_CIRCUIT_RECOVERY", "3"))
        return max(1, min(10, value))

    # Sandbox configuration
    @staticmethod
    def get_sandbox_dir() -> Path:
        default = Path(tempfile.gettempdir()) / "ouroboros" / "sandbox"
        return Path(os.getenv("OUROBOROS_SANDBOX_DIR", str(default)))

    @staticmethod
    def is_sandbox_enabled() -> bool:
        return os.getenv("OUROBOROS_SANDBOX_ENABLED", "true").lower() in ("true", "1", "yes")

    # Safe environment variables to pass to sandbox
    SAFE_ENV_VARS = {
        "PATH", "HOME", "USER", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
        "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
        "TERM", "COLORTERM", "TMPDIR", "TMP", "TEMP",
    }

    # Dangerous environment variable prefixes to exclude
    DANGEROUS_ENV_PREFIXES = {
        "AWS_", "GCP_", "GOOGLE_", "AZURE_", "GITHUB_", "GITLAB_",
        "API_KEY", "SECRET", "PASSWORD", "TOKEN", "CREDENTIAL",
        "ANTHROPIC_", "OPENAI_", "JARVIS_PRIME_API_KEY",
    }


# =============================================================================
# SECURITY UTILITIES
# =============================================================================

class SecurityValidator:
    """
    Security validation utilities.
    Prevents path traversal, command injection, and other attacks.
    """

    @staticmethod
    def validate_path(
        path: Union[str, Path],
        allowed_roots: Optional[List[Path]] = None,
    ) -> Path:
        """
        Validate and resolve a path, ensuring it's within allowed boundaries.

        Args:
            path: Path to validate
            allowed_roots: List of allowed root directories

        Returns:
            Canonical resolved path

        Raises:
            SecurityError: If path is outside allowed boundaries
        """
        if allowed_roots is None:
            allowed_roots = NativeConfig.ALLOWED_PATHS

        # Convert to Path and resolve to canonical form
        resolved = Path(path).resolve()

        # Check against allowed roots
        for root in allowed_roots:
            try:
                resolved_root = root.resolve()
                resolved.relative_to(resolved_root)
                return resolved  # Path is within this root
            except ValueError:
                continue  # Not within this root, try next

        raise SecurityError(
            f"Path '{path}' is outside allowed boundaries. "
            f"Allowed roots: {[str(r) for r in allowed_roots]}"
        )

    @staticmethod
    def validate_file_for_improvement(path: Path) -> None:
        """
        Validate a file is safe for self-improvement.

        Raises:
            SecurityError: If file is not safe
        """
        # Must exist
        if not path.exists():
            raise SecurityError(f"File does not exist: {path}")

        # Must be a file, not directory or symlink
        if path.is_symlink():
            raise SecurityError(f"Symbolic links not allowed: {path}")

        if not path.is_file():
            raise SecurityError(f"Path is not a file: {path}")

        # Check file size
        size = path.stat().st_size
        max_size = NativeConfig.get_max_file_size()
        if size > max_size:
            raise SecurityError(
                f"File too large: {size} bytes (max: {max_size})"
            )

        # Must be a Python file for code improvement
        if path.suffix not in (".py", ".pyi"):
            logger.warning(f"Non-Python file: {path.suffix}")

    @staticmethod
    def sanitize_for_shell(value: str) -> str:
        """
        Sanitize a value for safe shell usage.
        Uses shlex.quote for proper escaping.
        """
        return shlex.quote(value)

    @staticmethod
    def build_safe_command(
        executable: str,
        args: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[str], Dict[str, str]]:
        """
        Build a safe command without shell interpolation.

        Returns:
            Tuple of (command_list, safe_env)
        """
        # Validate executable exists
        exe_path = shutil.which(executable)
        if not exe_path:
            raise SecurityError(f"Executable not found: {executable}")

        # Build command list (no shell interpolation)
        cmd = [exe_path] + list(args)

        # Build safe environment
        safe_env = SecurityValidator.get_safe_environment()
        if env:
            # Only allow safe additional env vars
            for key, value in env.items():
                if SecurityValidator._is_safe_env_var(key):
                    safe_env[key] = value

        return cmd, safe_env

    @staticmethod
    def get_safe_environment() -> Dict[str, str]:
        """Get a sanitized environment dictionary."""
        safe_env = {}
        for key in NativeConfig.SAFE_ENV_VARS:
            if key in os.environ:
                safe_env[key] = os.environ[key]
        return safe_env

    @staticmethod
    def _is_safe_env_var(key: str) -> bool:
        """Check if an environment variable is safe to pass through."""
        upper_key = key.upper()
        for prefix in NativeConfig.DANGEROUS_ENV_PREFIXES:
            if upper_key.startswith(prefix):
                return False
        return True


class SecurityError(Exception):
    """Security validation failure."""
    pass


# =============================================================================
# THREAD-SAFE METRICS
# =============================================================================

class AtomicCounter:
    """Thread-safe atomic counter."""

    def __init__(self, initial: int = 0):
        self._value = initial
        self._lock = ThreadLock()

    def increment(self, amount: int = 1) -> int:
        with self._lock:
            self._value += amount
            return self._value

    def decrement(self, amount: int = 1) -> int:
        with self._lock:
            self._value -= amount
            return self._value

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def reset(self, value: int = 0) -> None:
        with self._lock:
            self._value = value


class ThreadSafeMetrics:
    """Thread-safe metrics collection with atomic operations."""

    def __init__(self):
        self._lock = ThreadLock()
        self._counters: Dict[str, AtomicCounter] = {}
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, List[float]] = {}
        self._histogram_max_size = 1000

    def increment(self, name: str, amount: int = 1) -> int:
        """Increment a counter atomically."""
        if name not in self._counters:
            with self._lock:
                if name not in self._counters:
                    self._counters[name] = AtomicCounter()
        return self._counters[name].increment(amount)

    def set_gauge(self, name: str, value: float) -> None:
        """Set a gauge value."""
        with self._lock:
            self._gauges[name] = value

    def record_histogram(self, name: str, value: float) -> None:
        """Record a value in a histogram."""
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = []
            hist = self._histograms[name]
            hist.append(value)
            # Keep bounded size
            if len(hist) > self._histogram_max_size:
                self._histograms[name] = hist[-self._histogram_max_size:]

    def get_counter(self, name: str) -> int:
        """Get counter value."""
        counter = self._counters.get(name)
        return counter.value if counter else 0

    def get_gauge(self, name: str) -> Optional[float]:
        """Get gauge value."""
        with self._lock:
            return self._gauges.get(name)

    def get_histogram_stats(self, name: str) -> Dict[str, float]:
        """Get histogram statistics."""
        with self._lock:
            hist = self._histograms.get(name, [])
            if not hist:
                return {}
            sorted_hist = sorted(hist)
            n = len(sorted_hist)
            return {
                "count": n,
                "min": sorted_hist[0],
                "max": sorted_hist[-1],
                "mean": sum(sorted_hist) / n,
                "p50": sorted_hist[n // 2],
                "p95": sorted_hist[int(n * 0.95)] if n >= 20 else sorted_hist[-1],
                "p99": sorted_hist[int(n * 0.99)] if n >= 100 else sorted_hist[-1],
            }

    def snapshot(self) -> Dict[str, Any]:
        """Get a snapshot of all metrics."""
        with self._lock:
            return {
                "counters": {k: v.value for k, v in self._counters.items()},
                "gauges": dict(self._gauges),
                "histograms": {
                    k: self.get_histogram_stats(k) for k in self._histograms
                },
            }


# =============================================================================
# IMPROVED CIRCUIT BREAKER
# =============================================================================

class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ImprovedCircuitBreaker:
    """
    Improved circuit breaker with proper state machine.

    Features:
    - Thread-safe state transitions
    - Configurable recovery threshold (N consecutive successes)
    - Per-request limiting in HALF_OPEN state
    - Metrics tracking
    """

    def __init__(
        self,
        name: str,
        failure_threshold: Optional[int] = None,
        recovery_timeout: Optional[float] = None,
        recovery_threshold: Optional[int] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold or NativeConfig.get_circuit_threshold()
        self.recovery_timeout = recovery_timeout or NativeConfig.get_circuit_timeout()
        self.recovery_threshold = recovery_threshold or NativeConfig.get_circuit_recovery_threshold()

        self._lock = asyncio.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._successes = 0
        self._half_open_requests = 0
        self._last_failure_time = 0.0
        self._last_success_time = 0.0

        self._metrics = ThreadSafeMetrics()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def can_execute(self) -> bool:
        """Check if request can be executed."""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # Check if recovery timeout elapsed
                if time.time() - self._last_failure_time >= self.recovery_timeout:
                    self._transition_to(CircuitState.HALF_OPEN)
                    self._half_open_requests = 0
                    return True
                return False

            # HALF_OPEN: Allow limited requests
            if self._half_open_requests < self.recovery_threshold:
                self._half_open_requests += 1
                return True
            return False

    async def record_success(self) -> None:
        """Record a successful execution."""
        async with self._lock:
            self._successes += 1
            self._last_success_time = time.time()
            self._metrics.increment("successes")

            if self._state == CircuitState.HALF_OPEN:
                # Need N consecutive successes to close
                if self._successes >= self.recovery_threshold:
                    self._transition_to(CircuitState.CLOSED)
                    self._failures = 0
                    self._successes = 0

    async def record_failure(self, error: Optional[str] = None) -> None:
        """Record a failed execution."""
        async with self._lock:
            self._failures += 1
            self._successes = 0  # Reset consecutive successes
            self._last_failure_time = time.time()
            self._metrics.increment("failures")

            if error:
                logger.warning(f"Circuit {self.name} failure: {error}")

            if self._state == CircuitState.HALF_OPEN:
                # Any failure in HALF_OPEN goes back to OPEN
                self._transition_to(CircuitState.OPEN)
            elif self._state == CircuitState.CLOSED:
                if self._failures >= self.failure_threshold:
                    self._transition_to(CircuitState.OPEN)

    def _transition_to(self, new_state: CircuitState) -> None:
        """Transition to a new state (must hold lock)."""
        old_state = self._state
        self._state = new_state
        logger.info(f"Circuit {self.name}: {old_state.value} -> {new_state.value}")
        self._metrics.increment(f"transitions_to_{new_state.value}")

    def get_status(self) -> Dict[str, Any]:
        """Get circuit breaker status."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failures": self._failures,
            "successes": self._successes,
            "last_failure": self._last_failure_time,
            "last_success": self._last_success_time,
            "metrics": self._metrics.snapshot(),
        }


# =============================================================================
# PROGRESS BROADCASTER
# =============================================================================

class ImprovementPhase(Enum):
    """Phases of self-improvement."""
    INITIALIZING = "initializing"
    ANALYZING = "analyzing"
    GENERATING = "generating"
    VALIDATING = "validating"
    TESTING = "testing"
    APPLYING = "applying"
    COMMITTING = "committing"
    LEARNING = "learning"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class ImprovementProgress:
    """Progress information for self-improvement."""
    task_id: str
    phase: ImprovementPhase
    target_file: str
    goal: str
    progress_percent: float = 0.0
    message: str = ""
    iteration: int = 0
    max_iterations: int = 0
    provider: str = ""
    error: Optional[str] = None
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "phase": self.phase.value,
            "target_file": self.target_file,
            "goal": self.goal[:100] + "..." if len(self.goal) > 100 else self.goal,
            "progress_percent": self.progress_percent,
            "message": self.message,
            "iteration": self.iteration,
            "max_iterations": self.max_iterations,
            "provider": self.provider,
            "error": self.error,
            "elapsed_seconds": time.time() - self.started_at,
        }


class ProgressBroadcaster:
    """
    Broadcasts improvement progress to the UI.

    Integrates with:
    - Menu bar status indicator
    - WebSocket connections
    - Event bus
    - Logging
    """

    def __init__(self):
        self._listeners: List[Callable[[ImprovementProgress], Awaitable[None]]] = []
        self._progress_history: Dict[str, ImprovementProgress] = {}
        self._lock = asyncio.Lock()
        self._max_history = 100

    def add_listener(
        self,
        callback: Callable[[ImprovementProgress], Awaitable[None]],
    ) -> Callable[[], None]:
        """
        Add a progress listener.

        Returns:
            A function to remove the listener
        """
        self._listeners.append(callback)

        def remove():
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    async def broadcast(self, progress: ImprovementProgress) -> None:
        """Broadcast progress to all listeners."""
        async with self._lock:
            self._progress_history[progress.task_id] = progress

            # Trim history
            if len(self._progress_history) > self._max_history:
                oldest_keys = sorted(
                    self._progress_history.keys(),
                    key=lambda k: self._progress_history[k].started_at
                )[:len(self._progress_history) - self._max_history]
                for key in oldest_keys:
                    del self._progress_history[key]

        # Log progress
        phase_icons = {
            ImprovementPhase.INITIALIZING: "🔧",
            ImprovementPhase.ANALYZING: "🔍",
            ImprovementPhase.GENERATING: "⚡",
            ImprovementPhase.VALIDATING: "✅",
            ImprovementPhase.TESTING: "🧪",
            ImprovementPhase.APPLYING: "📝",
            ImprovementPhase.COMMITTING: "💾",
            ImprovementPhase.LEARNING: "🧠",
            ImprovementPhase.COMPLETED: "✨",
            ImprovementPhase.FAILED: "❌",
        }
        icon = phase_icons.get(progress.phase, "⏳")
        logger.info(
            f"{icon} [{progress.task_id[:8]}] {progress.phase.value}: "
            f"{progress.message} ({progress.progress_percent:.0f}%)"
        )

        # Notify listeners
        for listener in self._listeners:
            try:
                await listener(progress)
            except Exception as e:
                logger.error(f"Progress listener error: {e}")

    async def get_active_improvements(self) -> List[ImprovementProgress]:
        """Get all active (non-completed) improvements."""
        async with self._lock:
            return [
                p for p in self._progress_history.values()
                if p.phase not in (ImprovementPhase.COMPLETED, ImprovementPhase.FAILED)
            ]

    async def get_progress(self, task_id: str) -> Optional[ImprovementProgress]:
        """Get progress for a specific task."""
        async with self._lock:
            return self._progress_history.get(task_id)


# =============================================================================
# NATIVE SELF-IMPROVEMENT ENGINE
# =============================================================================

@dataclass
class ImprovementRequest:
    """Request for self-improvement."""
    target_file: Path
    goal: str
    test_command: Optional[str] = None
    context: Optional[str] = None
    max_iterations: int = 5
    dry_run: bool = False
    auto_commit: bool = False


@dataclass
class ImprovementResult:
    """Result of self-improvement."""
    success: bool
    task_id: str
    target_file: str
    goal: str
    iterations: int
    total_time: float
    provider_used: str = ""
    changes_applied: bool = False
    error: Optional[str] = None
    diff: Optional[str] = None


class NativeSelfImprovement:
    """
    Native self-improvement engine integrated into JARVIS.

    This is the "motor function" - when JARVIS thinks "fix this bug",
    this class handles the actual execution transparently.

    Features:
    - Deep integration with JARVIS event loop
    - Progress broadcasting to UI
    - Thread-safe metrics
    - Secure sandbox execution
    - Circuit breaker protection
    - Cross-repo awareness
    """

    def __init__(self):
        self.logger = logging.getLogger("Ouroboros.NativeSelfImprovement")

        # Core components
        self._progress_broadcaster = ProgressBroadcaster()
        self._metrics = ThreadSafeMetrics()
        self._circuit_breakers: Dict[str, ImprovedCircuitBreaker] = {}

        # State
        self._running = False
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

        # Rate limiting
        self._rate_limiter: Optional[asyncio.Semaphore] = None
        self._last_request_time = 0.0

        # Cached integration reference - Trinity is the preferred integration layer
        self._trinity_integration = None
        self._legacy_integration = None  # Fallback for older integration.py
        self._brain_orchestrator = None

    async def initialize(self) -> None:
        """Initialize the native self-improvement engine."""
        self.logger.info("Initializing Native Self-Improvement Engine...")

        # Initialize rate limiter
        self._rate_limiter = asyncio.Semaphore(NativeConfig.get_burst_limit())

        # Initialize circuit breakers for providers
        provider_names = ["jarvis-prime", "ollama", "anthropic"]
        for name in provider_names:
            self._circuit_breakers[name] = ImprovedCircuitBreaker(name)

        # Try Trinity Integration first (preferred - production-grade)
        try:
            from backend.core.ouroboros.trinity_integration import (
                get_trinity_integration,
                initialize_trinity_integration,
            )
            self._trinity_integration = get_trinity_integration()
            await initialize_trinity_integration()
            self.logger.info("✅ Connected to Trinity Integration (production-grade)")
        except ImportError as e:
            self.logger.warning(f"Trinity Integration not available: {e}")
        except Exception as e:
            self.logger.warning(f"Trinity Integration init failed: {e}")

        # Connect to brain orchestrator if available (fallback)
        if not self._trinity_integration:
            try:
                from backend.core.ouroboros.brain_orchestrator import get_brain_orchestrator
                self._brain_orchestrator = get_brain_orchestrator()
                self.logger.info("✅ Connected to Brain Orchestrator (fallback)")
            except ImportError:
                self.logger.warning("Brain orchestrator not available")

        # Connect to legacy integration layer if available (final fallback)
        if not self._trinity_integration and not self._brain_orchestrator:
            try:
                from backend.core.ouroboros.integration import get_ouroboros_integration
                self._legacy_integration = get_ouroboros_integration()
                self.logger.info("✅ Connected to Legacy Integration (fallback)")
            except ImportError:
                self.logger.warning("Legacy integration layer not available")

        self._running = True
        self.logger.info("Native Self-Improvement Engine initialized")

    async def shutdown(self) -> None:
        """Shutdown the engine."""
        self.logger.info("Shutting down Native Self-Improvement Engine...")
        self._running = False

        # Cancel active tasks
        for task_id, task in list(self._active_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

        self._active_tasks.clear()
        self.logger.info("Native Self-Improvement Engine shutdown complete")

    @property
    def progress_broadcaster(self) -> ProgressBroadcaster:
        """Get the progress broadcaster for UI integration."""
        return self._progress_broadcaster

    async def execute_self_improvement(
        self,
        target: Union[str, Path],
        goal: str,
        test_command: Optional[str] = None,
        context: Optional[str] = None,
        max_iterations: int = 5,
        dry_run: bool = False,
        auto_commit: bool = False,
    ) -> ImprovementResult:
        """
        Execute self-improvement on a target file.

        This is the main entry point - called naturally from JARVIS:

            await execute_self_improvement(
                target="main.py",
                goal="Fix the race condition bug"
            )

        Args:
            target: Path to file to improve
            goal: Natural language description of the improvement
            test_command: Optional test command to validate
            context: Additional context about the improvement
            max_iterations: Maximum improvement attempts
            dry_run: If True, show changes without applying
            auto_commit: If True, commit changes to git

        Returns:
            ImprovementResult with success status and details
        """
        task_id = f"imp_{uuid.uuid4().hex[:12]}"
        start_time = time.time()

        # Create request
        try:
            target_path = SecurityValidator.validate_path(target)
            SecurityValidator.validate_file_for_improvement(target_path)
        except SecurityError as e:
            return ImprovementResult(
                success=False,
                task_id=task_id,
                target_file=str(target),
                goal=goal,
                iterations=0,
                total_time=time.time() - start_time,
                error=str(e),
            )

        request = ImprovementRequest(
            target_file=target_path,
            goal=goal,
            test_command=test_command,
            context=context,
            max_iterations=max_iterations,
            dry_run=dry_run,
            auto_commit=auto_commit,
        )

        # Broadcast initial progress
        progress = ImprovementProgress(
            task_id=task_id,
            phase=ImprovementPhase.INITIALIZING,
            target_file=str(target_path),
            goal=goal,
            progress_percent=0,
            message="Starting self-improvement...",
            max_iterations=max_iterations,
        )
        await self._progress_broadcaster.broadcast(progress)

        # Execute improvement
        try:
            result = await self._execute_improvement_loop(task_id, request, progress)

            # Record metrics
            self._metrics.increment("improvements_attempted")
            if result.success:
                self._metrics.increment("improvements_succeeded")
            else:
                self._metrics.increment("improvements_failed")
            self._metrics.record_histogram("improvement_duration", result.total_time)

            return result

        except asyncio.CancelledError:
            progress.phase = ImprovementPhase.FAILED
            progress.error = "Cancelled"
            await self._progress_broadcaster.broadcast(progress)
            raise
        except Exception as e:
            self.logger.error(f"Improvement failed: {e}", exc_info=True)
            progress.phase = ImprovementPhase.FAILED
            progress.error = str(e)
            await self._progress_broadcaster.broadcast(progress)

            return ImprovementResult(
                success=False,
                task_id=task_id,
                target_file=str(target_path),
                goal=goal,
                iterations=0,
                total_time=time.time() - start_time,
                error=str(e),
            )

    async def _execute_improvement_loop(
        self,
        task_id: str,
        request: ImprovementRequest,
        progress: ImprovementProgress,
    ) -> ImprovementResult:
        """Execute the improvement loop with Trinity v2.0 integration."""
        start_time = time.time()
        target_str = str(request.target_file)

        # =====================================================================
        # PRE-FLIGHT CHECKS (Trinity v2.0)
        # =====================================================================

        # Check 1: Circular dependency detection
        if self._trinity_integration:
            is_circular, reason = await self._trinity_integration.coordinator.check_circular_dependency(
                target_str
            )
            if is_circular:
                self.logger.warning(f"Circular dependency detected: {reason}")
                return ImprovementResult(
                    success=False,
                    task_id=task_id,
                    target_file=target_str,
                    goal=request.goal,
                    iterations=0,
                    total_time=time.time() - start_time,
                    error=f"Circular dependency: {reason}",
                )

        # Check 2: Learning cache - should we skip?
        if self._trinity_integration:
            should_skip, skip_reason = await self._trinity_integration.check_should_skip(
                target_str, request.goal
            )
            if should_skip:
                self.logger.info(f"Skipping improvement (learning cache): {skip_reason}")
                return ImprovementResult(
                    success=False,
                    task_id=task_id,
                    target_file=target_str,
                    goal=request.goal,
                    iterations=0,
                    total_time=time.time() - start_time,
                    error=f"Skipped: {skip_reason}",
                )

        # =====================================================================
        # ACQUIRE DISTRIBUTED LOCK (Trinity v2.0)
        # =====================================================================

        lock_acquired = True  # Default to True if no lock manager
        if self._trinity_integration:
            try:
                async with self._trinity_integration.acquire_lock(target_str) as acquired:
                    lock_acquired = acquired
                    if not acquired:
                        self.logger.warning(f"Could not acquire lock for: {target_str}")
                        return ImprovementResult(
                            success=False,
                            task_id=task_id,
                            target_file=target_str,
                            goal=request.goal,
                            iterations=0,
                            total_time=time.time() - start_time,
                            error="Could not acquire distributed lock - file may be locked by another process",
                        )

                    # Execute within lock context
                    return await self._execute_locked_improvement(
                        task_id, request, progress, start_time
                    )
            except Exception as e:
                self.logger.warning(f"Lock acquisition failed, proceeding without lock: {e}")
                # Fall through to execute without lock

        # Execute without lock (fallback or no Trinity integration)
        return await self._execute_locked_improvement(
            task_id, request, progress, start_time
        )

    async def _execute_locked_improvement(
        self,
        task_id: str,
        request: ImprovementRequest,
        progress: ImprovementProgress,
        start_time: float,
    ) -> ImprovementResult:
        """Execute improvement with lock already held."""
        target_str = str(request.target_file)

        # Phase 1: Analyze
        progress.phase = ImprovementPhase.ANALYZING
        progress.progress_percent = 10
        progress.message = f"Analyzing {request.target_file.name}..."
        await self._progress_broadcaster.broadcast(progress)

        original_content = await self._read_file_safe(request.target_file)

        # Phase 2: Generate improvements
        last_error = None
        improved_content = None
        provider_used = ""

        for iteration in range(1, request.max_iterations + 1):
            progress.phase = ImprovementPhase.GENERATING
            progress.iteration = iteration
            progress.progress_percent = 20 + (iteration / request.max_iterations * 40)
            progress.message = f"Generating improvement (attempt {iteration}/{request.max_iterations})..."
            await self._progress_broadcaster.broadcast(progress)

            try:
                improved_content, provider_used = await self._generate_improvement(
                    original_content,
                    request.goal,
                    last_error,
                    request.context,
                )
                progress.provider = provider_used

                if not improved_content:
                    last_error = "No improvement generated"
                    continue

                # Phase 3: Validate syntax
                progress.phase = ImprovementPhase.VALIDATING
                progress.progress_percent = 60
                progress.message = "Validating syntax..."
                await self._progress_broadcaster.broadcast(progress)

                valid, syntax_error = self._validate_syntax(improved_content)
                if not valid:
                    last_error = f"Syntax error: {syntax_error}"
                    continue

                # Phase 4: Test (if test command provided)
                if request.test_command:
                    progress.phase = ImprovementPhase.TESTING
                    progress.progress_percent = 70
                    progress.message = "Running tests..."
                    await self._progress_broadcaster.broadcast(progress)

                    test_passed, test_output = await self._run_tests_safe(
                        request.target_file,
                        improved_content,
                        request.test_command,
                    )

                    if not test_passed:
                        last_error = f"Tests failed: {test_output[:500]}"
                        continue

                # =========================================================
                # CODE REVIEW (Trinity v2.0 - Coding Council Integration)
                # =========================================================
                if self._trinity_integration:
                    try:
                        review = await self._trinity_integration.review_code(
                            original_code=original_content,
                            improved_code=improved_content,
                            goal=request.goal,
                            file_path=str(request.target_file),
                        )

                        # Import ReviewResult from trinity_integration
                        from backend.core.ouroboros.trinity_integration import ReviewResult

                        if review.result == ReviewResult.REJECTED:
                            last_error = f"Code review rejected: {review.feedback}"
                            self.logger.warning(f"Code review rejected: {review.feedback}")
                            if review.security_issues:
                                self.logger.warning(f"Security issues: {review.security_issues}")
                            continue

                        if review.result == ReviewResult.NEEDS_REVISION:
                            last_error = f"Code needs revision: {review.feedback}"
                            self.logger.info(f"Code needs revision: {review.suggestions}")
                            continue

                        # APPROVED - proceed with apply
                        self.logger.info(f"Code review passed (risk: {review.risk_score:.2f})")

                    except Exception as e:
                        self.logger.warning(f"Code review failed, proceeding: {e}")

                # Success! Apply changes
                if not request.dry_run:
                    progress.phase = ImprovementPhase.APPLYING
                    progress.progress_percent = 85
                    progress.message = "Applying changes..."
                    await self._progress_broadcaster.broadcast(progress)

                    # =========================================================
                    # APPLY WITH ROLLBACK (Trinity v2.0)
                    # =========================================================
                    if self._trinity_integration:
                        try:
                            async with self._trinity_integration.with_rollback(
                                request.target_file, task_id
                            ) as snapshot_ok:
                                if snapshot_ok:
                                    await self._write_file_safe(request.target_file, improved_content)
                                else:
                                    # No snapshot (file didn't exist?), write anyway
                                    await self._write_file_safe(request.target_file, improved_content)
                        except Exception as e:
                            # Rollback happened automatically
                            last_error = f"Apply failed (rolled back): {e}"
                            self.logger.error(f"Apply failed, rolled back: {e}")
                            continue
                    else:
                        await self._write_file_safe(request.target_file, improved_content)

                    # Auto-commit if requested
                    if request.auto_commit:
                        progress.phase = ImprovementPhase.COMMITTING
                        progress.message = "Committing changes..."
                        await self._progress_broadcaster.broadcast(progress)
                        await self._git_commit_safe(request.target_file, request.goal)

                # Phase 5: Learn
                progress.phase = ImprovementPhase.LEARNING
                progress.progress_percent = 95
                progress.message = "Recording experience..."
                await self._progress_broadcaster.broadcast(progress)

                await self._publish_experience(
                    original_content,
                    improved_content,
                    request.goal,
                    success=True,
                    iterations=iteration,
                    provider_used=provider_used,
                    duration_seconds=time.time() - start_time,
                )

                # =========================================================
                # RECORD SUCCESS IN LEARNING CACHE (Trinity v2.0)
                # =========================================================
                if self._trinity_integration:
                    await self._trinity_integration.record_improvement_attempt(
                        target=target_str,
                        goal=request.goal,
                        success=True,
                        error=None,
                    )

                # Complete!
                progress.phase = ImprovementPhase.COMPLETED
                progress.progress_percent = 100
                progress.message = "Improvement complete!"
                await self._progress_broadcaster.broadcast(progress)

                return ImprovementResult(
                    success=True,
                    task_id=task_id,
                    target_file=str(request.target_file),
                    goal=request.goal,
                    iterations=iteration,
                    total_time=time.time() - start_time,
                    provider_used=provider_used,
                    changes_applied=not request.dry_run,
                    diff=self._generate_diff(original_content, improved_content),
                )

            except Exception as e:
                last_error = str(e)
                self.logger.warning(f"Iteration {iteration} failed: {e}")
                continue

        # =====================================================================
        # ALL ITERATIONS FAILED - Trinity v2.0 Failure Handling
        # =====================================================================

        # Record failure in learning cache
        if self._trinity_integration:
            await self._trinity_integration.record_improvement_attempt(
                target=target_str,
                goal=request.goal,
                success=False,
                error=last_error,
            )

        # Queue for manual review if all automated attempts failed
        if self._trinity_integration:
            try:
                review_id = await self._trinity_integration.queue_for_manual_review(
                    target=target_str,
                    goal=request.goal,
                    original_code=original_content,
                    failure_reason=last_error or "Unknown failure after all iterations",
                    attempts=request.max_iterations,
                )
                self.logger.warning(
                    f"Improvement queued for manual review: {review_id}\n"
                    f"  Target: {target_str}\n"
                    f"  Goal: {request.goal[:100]}...\n"
                    f"  Reason: {last_error}"
                )
            except Exception as e:
                self.logger.error(f"Failed to queue for manual review: {e}")

        progress.phase = ImprovementPhase.FAILED
        progress.error = last_error
        await self._progress_broadcaster.broadcast(progress)

        return ImprovementResult(
            success=False,
            task_id=task_id,
            target_file=str(request.target_file),
            goal=request.goal,
            iterations=request.max_iterations,
            total_time=time.time() - start_time,
            provider_used=provider_used,
            error=last_error,
        )

    async def _generate_improvement(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str],
        context: Optional[str],
    ) -> Tuple[Optional[str], str]:
        """Generate improved code using available providers.

        Provider hierarchy (graceful degradation):
        1. Trinity Integration (preferred) - UnifiedModelServing + Neural Mesh
        2. Brain Orchestrator - Direct Ollama/Provider access
        3. Legacy Integration - Original integration.py
        """
        # Build prompt
        prompt = self._build_improvement_prompt(original_code, goal, error_log, context)
        system_prompt = (
            "You are an expert Python developer. You improve code based on goals "
            "and fix errors. Return ONLY valid Python code in ```python blocks."
        )

        # Strategy 1: Trinity Integration (preferred - production-grade)
        if self._trinity_integration:
            try:
                content, provider = await self._trinity_integration.generate_improvement(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    temperature=0.3,
                    max_tokens=4096,
                )
                if content:
                    extracted = self._extract_code(content)
                    if extracted:
                        return extracted, f"trinity:{provider}"
            except Exception as e:
                self.logger.warning(f"Trinity Integration failed: {e}")

        # Strategy 2: Brain Orchestrator (fallback)
        if self._brain_orchestrator:
            provider = self._brain_orchestrator.get_best_provider()
            if provider and provider.is_healthy:
                circuit = self._circuit_breakers.get(provider.name)
                if circuit and await circuit.can_execute():
                    try:
                        result = await self._call_provider(
                            provider.endpoint,
                            prompt,
                        )
                        await circuit.record_success()
                        extracted = self._extract_code(result)
                        if extracted:
                            return extracted, f"brain:{provider.name}"
                    except Exception as e:
                        await circuit.record_failure(str(e))

        # Strategy 3: Legacy Integration (final fallback)
        if self._legacy_integration:
            try:
                result = await self._legacy_integration.generate_improvement(
                    original_code=original_code,
                    goal=goal,
                    error_log=error_log,
                    context=context,
                )
                if result:
                    return result, "legacy"
            except Exception as e:
                self.logger.warning(f"Legacy integration failed: {e}")

        return None, ""

    def _build_improvement_prompt(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str],
        context: Optional[str],
    ) -> str:
        """Build the improvement prompt."""
        parts = [
            "You are an expert Python developer. Improve the following code.",
            "",
            "## Goal",
            goal,
            "",
            "## Original Code",
            "```python",
            original_code,
            "```",
            "",
        ]

        if error_log:
            parts.extend([
                "## Previous Error (fix this)",
                "```",
                error_log[:2000],
                "```",
                "",
            ])

        if context:
            parts.extend([
                "## Additional Context",
                context[:3000],
                "",
            ])

        parts.extend([
            "## Instructions",
            "1. Return ONLY the improved Python code",
            "2. Wrap the code in ```python ... ``` markers",
            "3. Preserve all existing functionality",
            "4. Add comments only where necessary",
            "5. Follow PEP 8 style guidelines",
        ])

        return "\n".join(parts)

    async def _call_provider(
        self,
        endpoint: str,
        prompt: str,
    ) -> str:
        """Call an LLM provider."""
        if not aiohttp:
            raise RuntimeError("aiohttp not available")

        async with aiohttp.ClientSession() as session:
            payload = {
                "model": "default",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 4096,
            }

            timeout = aiohttp.ClientTimeout(total=NativeConfig.get_llm_timeout())
            url = f"{endpoint.rstrip('/')}/v1/chat/completions"

            async with session.post(url, json=payload, timeout=timeout) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Provider error ({resp.status}): {text[:500]}")

                data = await resp.json()
                choices = data.get("choices", [])
                if not choices:
                    raise RuntimeError("No choices in response")

                message = choices[0].get("message", {})
                content = message.get("content", "")
                if not content:
                    raise RuntimeError("Empty content in response")

                return content

    def _extract_code(self, response: str) -> str:
        """Extract code from LLM response."""
        # Prefer python-tagged blocks, then any code block, then raw response
        patterns = [
            r"```python\s*([\s\S]*?)```",
            r"```\s*([\s\S]*?)```",
        ]

        for pattern in patterns:
            matches = list(re.finditer(pattern, response))
            if matches:
                # Return the LAST match (model often puts final code at end)
                return matches[-1].group(1).strip()

        return response.strip()

    def _validate_syntax(self, code: str) -> Tuple[bool, Optional[str]]:
        """Validate Python syntax."""
        import ast
        try:
            ast.parse(code)
            return True, None
        except SyntaxError as e:
            return False, f"Line {e.lineno}: {e.msg}"
        except Exception as e:
            return False, str(e)

    async def _read_file_safe(self, path: Path) -> str:
        """Safely read a file with size limits."""
        # Validate path again
        path = SecurityValidator.validate_path(path)

        # Check size
        size = path.stat().st_size
        if size > NativeConfig.get_max_file_size():
            raise SecurityError(f"File too large: {size} bytes")

        # Read with explicit encoding
        if aiofiles:
            async with aiofiles.open(path, "r", encoding="utf-8") as f:
                return await f.read()
        else:
            return await asyncio.to_thread(path.read_text, "utf-8")

    async def _write_file_safe(self, path: Path, content: str) -> None:
        """Safely write a file."""
        # Validate path
        path = SecurityValidator.validate_path(path)

        if aiofiles:
            async with aiofiles.open(path, "w", encoding="utf-8") as f:
                await f.write(content)
        else:
            await asyncio.to_thread(path.write_text, content, "utf-8")

    async def _run_tests_safe(
        self,
        target_file: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """Run tests safely in sandbox."""
        if not NativeConfig.is_sandbox_enabled():
            self.logger.warning("Sandbox disabled - skipping test validation")
            return True, "Sandbox disabled"

        # Create sandbox directory
        sandbox_dir = NativeConfig.get_sandbox_dir() / f"sandbox_{uuid.uuid4().hex[:8]}"
        sandbox_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Copy file to sandbox
            sandbox_file = sandbox_dir / target_file.name
            sandbox_file.write_text(modified_content, encoding="utf-8")

            # Build safe command
            cmd, env = SecurityValidator.build_safe_command(
                "python",
                ["-m", "pytest", str(sandbox_file), "-v", "--tb=short"],
                {"PYTHONPATH": str(sandbox_dir)},
            )

            # Execute with timeout
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=sandbox_dir,
                env=env,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=NativeConfig.get_test_timeout(),
                )

                output = stdout.decode()[:NativeConfig.get_max_output_size()]
                output += stderr.decode()[:NativeConfig.get_max_output_size()]

                return process.returncode == 0, output

            except asyncio.TimeoutError:
                # Graceful termination
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                return False, "Test timeout"

        finally:
            # Cleanup sandbox
            try:
                shutil.rmtree(sandbox_dir)
            except Exception:
                pass

    async def _git_commit_safe(self, file_path: Path, goal: str) -> None:
        """Safely commit changes to git."""
        # Build safe commit message
        message = f"[Ouroboros] {goal[:100]}"

        cmd, env = SecurityValidator.build_safe_command(
            "git",
            ["commit", "-m", message, "--", str(file_path)],
        )

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=file_path.parent,
            env=env,
        )

        await process.communicate()

    async def _publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        provider_used: str = "",
        duration_seconds: float = 0.0,
    ) -> None:
        """Publish improvement experience to Reactor Core.

        Uses multi-channel publishing for reliability:
        1. Trinity Integration (preferred) - CrossRepoExperienceForwarder + Neural Mesh
        2. Legacy Integration (fallback) - Original integration.py
        """
        # Strategy 1: Trinity Integration (preferred - multi-channel)
        if self._trinity_integration:
            try:
                await self._trinity_integration.publish_experience(
                    original_code=original_code,
                    improved_code=improved_code,
                    goal=goal,
                    success=success,
                    iterations=iterations,
                    provider_used=provider_used,
                    duration_seconds=duration_seconds,
                )
                return  # Success - Trinity handles fallbacks internally
            except Exception as e:
                self.logger.warning(f"Trinity experience publish failed: {e}")

        # Strategy 2: Legacy Integration (fallback)
        if self._legacy_integration:
            try:
                await self._legacy_integration.publish_experience(
                    original_code=original_code,
                    improved_code=improved_code,
                    goal=goal,
                    success=success,
                    iterations=iterations,
                )
            except Exception as e:
                self.logger.warning(f"Legacy experience publish failed: {e}")

    def _generate_diff(self, original: str, modified: str) -> str:
        """Generate a simple diff."""
        import difflib
        diff = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile="original",
            tofile="modified",
        )
        return "".join(diff)

    def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        status = {
            "running": self._running,
            "active_tasks": len(self._active_tasks),
            "metrics": self._metrics.snapshot(),
            "circuit_breakers": {
                name: cb.get_status()
                for name, cb in self._circuit_breakers.items()
            },
            "integration": {
                "trinity_available": self._trinity_integration is not None,
                "brain_orchestrator_available": self._brain_orchestrator is not None,
                "legacy_integration_available": self._legacy_integration is not None,
            },
        }

        # Add Trinity Integration status if available
        if self._trinity_integration:
            try:
                status["trinity"] = self._trinity_integration.get_status()
            except Exception:
                status["trinity"] = {"error": "Unable to retrieve status"}

        return status


# =============================================================================
# GLOBAL INSTANCE & CONVENIENCE FUNCTIONS
# =============================================================================

_native_engine: Optional[NativeSelfImprovement] = None


def get_native_self_improvement() -> NativeSelfImprovement:
    """Get the global native self-improvement engine."""
    global _native_engine
    if _native_engine is None:
        _native_engine = NativeSelfImprovement()
    return _native_engine


async def initialize_native_self_improvement() -> None:
    """Initialize the native self-improvement engine."""
    engine = get_native_self_improvement()
    await engine.initialize()


async def shutdown_native_self_improvement() -> None:
    """Shutdown the native self-improvement engine."""
    global _native_engine
    if _native_engine:
        await _native_engine.shutdown()
        _native_engine = None


def is_native_self_improvement_running() -> bool:
    """
    Check if the native self-improvement engine exists and is running
    WITHOUT creating a new instance.

    This is critical for probes/health checks that need to verify state
    without side effects.

    Returns:
        True if engine exists and is running, False otherwise
    """
    return _native_engine is not None and getattr(_native_engine, '_running', False)


def get_native_self_improvement_if_exists() -> Optional[NativeSelfImprovement]:
    """
    Get the native self-improvement instance if it exists, without creating a new one.

    Returns:
        The existing engine instance or None if not initialized
    """
    return _native_engine


async def execute_self_improvement(
    target: Union[str, Path],
    goal: str,
    test_command: Optional[str] = None,
    context: Optional[str] = None,
    max_iterations: int = 5,
    dry_run: bool = False,
    auto_commit: bool = False,
) -> ImprovementResult:
    """
    Execute self-improvement on a target file.

    This is the main convenience function - the "motor function" of JARVIS.

    Example:
        result = await execute_self_improvement(
            target="backend/core/utils.py",
            goal="Fix the race condition in the cache update"
        )

        if result.success:
            print(f"Fixed in {result.iterations} iteration(s)!")
    """
    engine = get_native_self_improvement()
    if not engine._running:
        await engine.initialize()

    return await engine.execute_self_improvement(
        target=target,
        goal=goal,
        test_command=test_command,
        context=context,
        max_iterations=max_iterations,
        dry_run=dry_run,
        auto_commit=auto_commit,
    )


# =============================================================================
# CLAUDE CODE-LIKE BEHAVIORS v1.0
# =============================================================================
# These additions provide Claude Code-like features:
# 1. Diff Preview - Show changes before applying with user approval
# 2. Multi-File Orchestration - Atomic editing of multiple related files
# 3. Session Memory - Track what changed in current session
# 4. Streaming Changes - Real-time diff updates to UI
# 5. Iterative Refinement - User-directed change refinement loop
# 6. Connection Pooling - Efficient API connection reuse
# =============================================================================


@dataclass
class DiffChunk:
    """A chunk of a diff for streaming."""
    file_path: str
    line_start: int
    line_end: int
    old_content: str
    new_content: str
    change_type: str  # 'add', 'remove', 'modify'
    context_before: List[str] = field(default_factory=list)
    context_after: List[str] = field(default_factory=list)


@dataclass
class DiffPreview:
    """Complete diff preview for user approval with git integration."""
    id: str
    files: Dict[str, List[DiffChunk]]
    total_additions: int = 0
    total_deletions: int = 0
    total_modifications: int = 0
    risk_score: float = 0.0
    affected_entities: List[str] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    # v3.1: Git integration fields
    git_tracked: bool = False
    git_root: Optional[str] = None
    relative_path: Optional[str] = None
    commit_message: Optional[str] = None
    branch_name: Optional[str] = None

    def __post_init__(self):
        if self.expires_at == 0.0:
            # Preview expires after 5 minutes
            self.expires_at = self.generated_at + 300.0

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def file_count(self) -> int:
        """Number of files in this preview."""
        return len(self.files)

    @property
    def total_changes(self) -> int:
        """Total number of changes across all files."""
        return self.total_additions + self.total_deletions + self.total_modifications

    def to_unified_diff(self) -> str:
        """Generate unified diff string for display."""
        lines = []
        for file_path, chunks in self.files.items():
            lines.append(f"--- a/{file_path}")
            lines.append(f"+++ b/{file_path}")
            for chunk in chunks:
                lines.append(f"@@ -{chunk.line_start},{len(chunk.old_content.splitlines())} "
                           f"+{chunk.line_start},{len(chunk.new_content.splitlines())} @@")
                for ctx in chunk.context_before:
                    lines.append(f" {ctx}")
                for old_line in chunk.old_content.splitlines():
                    lines.append(f"-{old_line}")
                for new_line in chunk.new_content.splitlines():
                    lines.append(f"+{new_line}")
                for ctx in chunk.context_after:
                    lines.append(f" {ctx}")
        return "\n".join(lines)

    def to_git_diff_format(self) -> str:
        """Generate git-style diff format with headers."""
        lines = []

        for file_path, chunks in self.files.items():
            # Git diff header
            rel_path = self.relative_path if self.relative_path else file_path
            lines.append(f"diff --git a/{rel_path} b/{rel_path}")
            lines.append(f"--- a/{rel_path}")
            lines.append(f"+++ b/{rel_path}")

            for chunk in chunks:
                # Hunk header
                old_count = len(chunk.old_content.splitlines()) if chunk.old_content else 0
                new_count = len(chunk.new_content.splitlines()) if chunk.new_content else 0
                lines.append(f"@@ -{chunk.line_start},{old_count} +{chunk.line_start},{new_count} @@")

                # Context before
                for ctx in chunk.context_before:
                    lines.append(f" {ctx}")

                # Changes
                for old_line in chunk.old_content.splitlines():
                    lines.append(f"-{old_line}")
                for new_line in chunk.new_content.splitlines():
                    lines.append(f"+{new_line}")

                # Context after
                for ctx in chunk.context_after:
                    lines.append(f" {ctx}")

        return "\n".join(lines)

    def to_summary(self) -> Dict[str, Any]:
        """Generate a summary dict for UI display."""
        return {
            "id": self.id,
            "file_count": self.file_count,
            "total_additions": self.total_additions,
            "total_deletions": self.total_deletions,
            "total_modifications": self.total_modifications,
            "total_changes": self.total_changes,
            "risk_score": self.risk_score,
            "risk_level": "high" if self.risk_score > 0.7 else "medium" if self.risk_score > 0.3 else "low",
            "git_tracked": self.git_tracked,
            "commit_message": self.commit_message,
            "is_expired": self.is_expired,
            "files": list(self.files.keys()),
        }


class DiffPreviewEngine:
    """
    Provides diff preview before applying changes - Claude Code-like behavior.

    Flow:
    1. Generate improvement
    2. Create diff preview
    3. Stream diff to user
    4. Wait for user approval/rejection/modification request
    5. Apply or iterate based on feedback
    """

    def __init__(self):
        self._pending_previews: Dict[str, DiffPreview] = {}
        self._approval_callbacks: Dict[str, asyncio.Event] = {}
        self._approval_results: Dict[str, Tuple[bool, Optional[str]]] = {}
        self._lock = asyncio.Lock()
        self._stream_listeners: List[Callable[[DiffChunk], Awaitable[None]]] = []

    def add_stream_listener(
        self, callback: Callable[[DiffChunk], Awaitable[None]]
    ) -> Callable[[], None]:
        """Add a listener for streaming diff chunks."""
        self._stream_listeners.append(callback)
        return lambda: self._stream_listeners.remove(callback) if callback in self._stream_listeners else None

    async def create_preview(
        self,
        original_content: str,
        modified_content: str,
        file_path: str,
        goal: str,
    ) -> DiffPreview:
        """Create a diff preview for user approval."""
        import difflib

        preview_id = f"preview_{uuid.uuid4().hex[:12]}"

        # Parse both versions
        original_lines = original_content.splitlines(keepends=True)
        modified_lines = modified_content.splitlines(keepends=True)

        # Generate unified diff
        differ = difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            lineterm=""
        )
        diff_lines = list(differ)

        # Parse diff into chunks
        chunks = self._parse_diff_chunks(diff_lines, original_lines, modified_lines)

        # Calculate statistics
        additions = sum(1 for c in chunks if c.change_type == 'add')
        deletions = sum(1 for c in chunks if c.change_type == 'remove')
        modifications = sum(1 for c in chunks if c.change_type == 'modify')

        # Calculate risk score based on change magnitude
        total_original_lines = len(original_lines)
        total_changes = additions + deletions + modifications
        risk_score = min(1.0, total_changes / max(total_original_lines, 1) * 2)

        preview = DiffPreview(
            id=preview_id,
            files={file_path: chunks},
            total_additions=additions,
            total_deletions=deletions,
            total_modifications=modifications,
            risk_score=risk_score,
        )

        async with self._lock:
            self._pending_previews[preview_id] = preview
            self._approval_callbacks[preview_id] = asyncio.Event()

        return preview

    def _parse_diff_chunks(
        self,
        diff_lines: List[str],
        original_lines: List[str],
        modified_lines: List[str],
    ) -> List[DiffChunk]:
        """Parse unified diff into structured chunks."""
        chunks = []
        current_chunk = None
        line_num = 0

        for line in diff_lines:
            if line.startswith('@@'):
                # Parse hunk header
                import re
                match = re.match(r'@@ -(\d+),?\d* \+(\d+),?\d* @@', line)
                if match:
                    if current_chunk:
                        chunks.append(current_chunk)
                    line_num = int(match.group(1))
                    current_chunk = DiffChunk(
                        file_path="",
                        line_start=line_num,
                        line_end=line_num,
                        old_content="",
                        new_content="",
                        change_type="modify",
                    )
            elif current_chunk is not None:
                if line.startswith('-') and not line.startswith('---'):
                    current_chunk.old_content += line[1:] + "\n"
                    current_chunk.change_type = "remove" if not current_chunk.new_content else "modify"
                elif line.startswith('+') and not line.startswith('+++'):
                    current_chunk.new_content += line[1:] + "\n"
                    current_chunk.change_type = "add" if not current_chunk.old_content else "modify"
                elif line.startswith(' '):
                    if not current_chunk.old_content and not current_chunk.new_content:
                        current_chunk.context_before.append(line[1:])
                    else:
                        current_chunk.context_after.append(line[1:])
                    line_num += 1

        if current_chunk and (current_chunk.old_content or current_chunk.new_content):
            chunks.append(current_chunk)

        return chunks

    # =========================================================================
    # v3.1: Git Integration for Proper Diff Format
    # =========================================================================

    async def is_git_tracked(self, file_path: str) -> bool:
        """Check if a file is tracked in a git repository."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "ls-files", "--error-unmatch", file_path,
                cwd=os.path.dirname(file_path) or ".",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception:
            return False

    async def get_git_root(self, file_path: str) -> Optional[str]:
        """Get the git repository root for a file."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "--show-toplevel",
                cwd=os.path.dirname(file_path) or ".",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return None

    async def create_git_preview(
        self,
        original_content: str,
        modified_content: str,
        file_path: str,
        goal: str,
    ) -> DiffPreview:
        """
        Create a diff preview using git for proper formatting.

        Falls back to difflib if file is not git-tracked.
        """
        # Check if file is in a git repo
        git_root = await self.get_git_root(file_path)
        if git_root is None:
            # Fall back to standard diff
            return await self.create_preview(original_content, modified_content, file_path, goal)

        preview_id = f"git_preview_{uuid.uuid4().hex[:12]}"

        # Get relative path for git
        abs_path = os.path.abspath(file_path)
        rel_path = os.path.relpath(abs_path, git_root)

        # Create temp file with modified content for git diff
        import tempfile
        diff_output = ""

        try:
            # Write original to temp file to compare
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp_orig:
                tmp_orig.write(original_content)
                tmp_orig_path = tmp_orig.name

            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as tmp_mod:
                tmp_mod.write(modified_content)
                tmp_mod_path = tmp_mod.name

            # Use git diff with no-index for comparing arbitrary files
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--no-index", "--no-color",
                f"--src-prefix=a/", f"--dst-prefix=b/",
                tmp_orig_path, tmp_mod_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            diff_output = stdout.decode()

            # Replace temp paths with actual file path in output
            diff_output = diff_output.replace(tmp_orig_path, rel_path)
            diff_output = diff_output.replace(tmp_mod_path, rel_path)

        except Exception as e:
            logger.warning(f"Git diff failed, falling back to difflib: {e}")
            return await self.create_preview(original_content, modified_content, file_path, goal)
        finally:
            # Cleanup temp files
            for tmp in [tmp_orig_path, tmp_mod_path]:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass

        # Parse git diff output into chunks
        chunks = self._parse_git_diff(diff_output, file_path)

        # Calculate statistics
        additions = sum(1 for c in chunks if c.change_type == 'add')
        deletions = sum(1 for c in chunks if c.change_type == 'remove')
        modifications = sum(1 for c in chunks if c.change_type == 'modify')

        # Calculate risk score
        original_lines = len(original_content.splitlines())
        total_changes = additions + deletions + modifications
        risk_score = min(1.0, total_changes / max(original_lines, 1) * 2)

        preview = DiffPreview(
            id=preview_id,
            files={file_path: chunks},
            total_additions=additions,
            total_deletions=deletions,
            total_modifications=modifications,
            risk_score=risk_score,
            git_tracked=True,
            git_root=git_root,
            relative_path=rel_path,
        )

        async with self._lock:
            self._pending_previews[preview_id] = preview
            self._approval_callbacks[preview_id] = asyncio.Event()

        return preview

    def _parse_git_diff(self, diff_output: str, file_path: str) -> List[DiffChunk]:
        """Parse git diff output into DiffChunk objects."""
        chunks = []
        current_chunk = None
        in_hunk = False

        for line in diff_output.splitlines():
            if line.startswith('@@'):
                # New hunk - save previous chunk
                if current_chunk and (current_chunk.old_content or current_chunk.new_content):
                    chunks.append(current_chunk)

                # Parse hunk header: @@ -start,count +start,count @@
                import re
                match = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
                if match:
                    old_start = int(match.group(1))
                    new_start = int(match.group(2))
                    current_chunk = DiffChunk(
                        file_path=file_path,
                        line_start=old_start,
                        line_end=old_start,
                        old_content="",
                        new_content="",
                        change_type="modify",
                    )
                    in_hunk = True
            elif in_hunk and current_chunk is not None:
                if line.startswith('-') and not line.startswith('---'):
                    # Removed line
                    current_chunk.old_content += line[1:] + "\n"
                    current_chunk.line_end += 1
                    if not current_chunk.new_content:
                        current_chunk.change_type = "remove"
                elif line.startswith('+') and not line.startswith('+++'):
                    # Added line
                    current_chunk.new_content += line[1:] + "\n"
                    if not current_chunk.old_content:
                        current_chunk.change_type = "add"
                    else:
                        current_chunk.change_type = "modify"
                elif line.startswith(' '):
                    # Context line
                    if not current_chunk.old_content and not current_chunk.new_content:
                        current_chunk.context_before.append(line[1:])
                    else:
                        current_chunk.context_after.append(line[1:])
            elif line.startswith('diff --git'):
                # Start of new file diff - reset state
                if current_chunk and (current_chunk.old_content or current_chunk.new_content):
                    chunks.append(current_chunk)
                current_chunk = None
                in_hunk = False

        # Don't forget the last chunk
        if current_chunk and (current_chunk.old_content or current_chunk.new_content):
            chunks.append(current_chunk)

        return chunks

    async def get_git_status(self, file_path: str) -> Dict[str, Any]:
        """Get detailed git status for a file."""
        result = {
            "tracked": False,
            "staged": False,
            "modified": False,
            "untracked": False,
            "git_root": None,
            "relative_path": None,
        }

        git_root = await self.get_git_root(file_path)
        if git_root is None:
            return result

        result["git_root"] = git_root
        abs_path = os.path.abspath(file_path)
        result["relative_path"] = os.path.relpath(abs_path, git_root)

        try:
            # Get porcelain status
            proc = await asyncio.create_subprocess_exec(
                "git", "status", "--porcelain", result["relative_path"],
                cwd=git_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                status_line = stdout.decode().strip()
                if status_line:
                    xy = status_line[:2]
                    index_status = xy[0]
                    worktree_status = xy[1]

                    result["tracked"] = index_status != '?' and worktree_status != '?'
                    result["staged"] = index_status in 'MADRCU'
                    result["modified"] = worktree_status in 'M'
                    result["untracked"] = xy == '??'
                else:
                    # Empty output means clean and tracked
                    result["tracked"] = await self.is_git_tracked(file_path)
        except Exception as e:
            logger.warning(f"Failed to get git status: {e}")

        return result

    async def stage_changes(self, file_path: str) -> bool:
        """Stage changes to git (git add)."""
        git_root = await self.get_git_root(file_path)
        if git_root is None:
            return False

        try:
            abs_path = os.path.abspath(file_path)
            rel_path = os.path.relpath(abs_path, git_root)

            proc = await asyncio.create_subprocess_exec(
                "git", "add", rel_path,
                cwd=git_root,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0
        except Exception as e:
            logger.error(f"Failed to stage changes: {e}")
            return False

    async def get_head_version(self, file_path: str) -> Optional[str]:
        """Get the HEAD version of a file from git."""
        git_root = await self.get_git_root(file_path)
        if git_root is None:
            return None

        try:
            abs_path = os.path.abspath(file_path)
            rel_path = os.path.relpath(abs_path, git_root)

            proc = await asyncio.create_subprocess_exec(
                "git", "show", f"HEAD:{rel_path}",
                cwd=git_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()

            if proc.returncode == 0:
                return stdout.decode()
        except Exception as e:
            logger.warning(f"Failed to get HEAD version: {e}")

        return None

    async def create_commit_preview(
        self,
        files: Dict[str, Tuple[str, str]],  # path -> (original, modified)
        commit_message: str,
    ) -> DiffPreview:
        """
        Create a preview for a multi-file commit.

        Args:
            files: Dict mapping file paths to (original_content, modified_content) tuples
            commit_message: Proposed commit message

        Returns:
            DiffPreview with all file changes consolidated
        """
        preview_id = f"commit_preview_{uuid.uuid4().hex[:12]}"
        all_chunks: Dict[str, List[DiffChunk]] = {}
        total_additions = 0
        total_deletions = 0
        total_modifications = 0

        for file_path, (original, modified) in files.items():
            # Use git diff for each file
            sub_preview = await self.create_git_preview(original, modified, file_path, commit_message)
            if sub_preview.files:
                all_chunks.update(sub_preview.files)
                total_additions += sub_preview.total_additions
                total_deletions += sub_preview.total_deletions
                total_modifications += sub_preview.total_modifications

        # Calculate overall risk score
        total_changes = total_additions + total_deletions + total_modifications
        risk_score = min(1.0, total_changes / 100)  # Scale by 100 lines

        preview = DiffPreview(
            id=preview_id,
            files=all_chunks,
            total_additions=total_additions,
            total_deletions=total_deletions,
            total_modifications=total_modifications,
            risk_score=risk_score,
            commit_message=commit_message,
        )

        async with self._lock:
            self._pending_previews[preview_id] = preview
            self._approval_callbacks[preview_id] = asyncio.Event()

        return preview

    async def stream_preview(self, preview: DiffPreview) -> None:
        """Stream diff chunks to all listeners."""
        for file_path, chunks in preview.files.items():
            for chunk in chunks:
                chunk.file_path = file_path
                for listener in self._stream_listeners:
                    try:
                        await listener(chunk)
                    except Exception as e:
                        logger.warning(f"Stream listener error: {e}")
                # Small delay for visual effect
                await asyncio.sleep(0.05)

    async def wait_for_approval(
        self,
        preview_id: str,
        timeout: float = 300.0,
    ) -> Tuple[bool, Optional[str]]:
        """
        Wait for user approval of a preview.

        Returns:
            Tuple of (approved, feedback)
            - approved=True, feedback=None: Apply changes
            - approved=False, feedback=None: Reject changes
            - approved=False, feedback="...": Request modifications
        """
        async with self._lock:
            event = self._approval_callbacks.get(preview_id)
            if not event:
                return False, "Preview not found or expired"

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            async with self._lock:
                result = self._approval_results.get(preview_id, (False, "No response"))
                # Cleanup
                self._pending_previews.pop(preview_id, None)
                self._approval_callbacks.pop(preview_id, None)
                self._approval_results.pop(preview_id, None)
                return result
        except asyncio.TimeoutError:
            async with self._lock:
                self._pending_previews.pop(preview_id, None)
                self._approval_callbacks.pop(preview_id, None)
            return False, "Approval timeout"

    async def submit_approval(
        self,
        preview_id: str,
        approved: bool,
        feedback: Optional[str] = None,
    ) -> bool:
        """Submit user's approval decision."""
        async with self._lock:
            event = self._approval_callbacks.get(preview_id)
            if not event:
                return False

            self._approval_results[preview_id] = (approved, feedback)
            event.set()
            return True

    def get_pending_previews(self) -> List[DiffPreview]:
        """Get all pending previews awaiting approval."""
        return [p for p in self._pending_previews.values() if not p.is_expired]


class SessionMemoryManager:
    """
    Tracks changes made within the current session.

    Provides:
    - What files were modified
    - What changes were made
    - Session-scoped rollback capability
    - Cross-reference between changes
    """

    def __init__(self):
        self._session_id = f"session_{uuid.uuid4().hex[:12]}"
        self._started_at = time.time()
        self._changes: List[Dict[str, Any]] = []
        self._file_snapshots: Dict[str, str] = {}  # path -> original content
        self._lock = asyncio.Lock()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def duration_seconds(self) -> float:
        return time.time() - self._started_at

    async def record_change(
        self,
        file_path: str,
        original_content: str,
        new_content: str,
        change_type: str,
        goal: str,
        success: bool,
    ) -> None:
        """Record a change made in this session."""
        async with self._lock:
            # Store original snapshot if first change to this file
            if file_path not in self._file_snapshots:
                self._file_snapshots[file_path] = original_content

            self._changes.append({
                "id": f"change_{uuid.uuid4().hex[:8]}",
                "file_path": file_path,
                "change_type": change_type,
                "goal": goal,
                "success": success,
                "timestamp": time.time(),
                "lines_added": len(new_content.splitlines()) - len(original_content.splitlines()),
                "original_hash": hashlib.md5(original_content.encode()).hexdigest()[:12],
                "new_hash": hashlib.md5(new_content.encode()).hexdigest()[:12],
            })

    async def get_session_summary(self) -> Dict[str, Any]:
        """Get a summary of the current session."""
        async with self._lock:
            files_modified = set(c["file_path"] for c in self._changes)
            successful = sum(1 for c in self._changes if c["success"])
            failed = len(self._changes) - successful

            return {
                "session_id": self._session_id,
                "duration_seconds": self.duration_seconds,
                "total_changes": len(self._changes),
                "files_modified": list(files_modified),
                "successful_changes": successful,
                "failed_changes": failed,
                "changes": self._changes[-10:],  # Last 10 changes
            }

    async def get_original_content(self, file_path: str) -> Optional[str]:
        """Get the original content of a file before any changes."""
        async with self._lock:
            return self._file_snapshots.get(file_path)

    async def rollback_session(self) -> Dict[str, bool]:
        """Rollback all changes in this session to original state."""
        results = {}
        async with self._lock:
            for file_path, original_content in self._file_snapshots.items():
                try:
                    path = Path(file_path)
                    if path.exists():
                        path.write_text(original_content, encoding="utf-8")
                        results[file_path] = True
                except Exception as e:
                    logger.error(f"Rollback failed for {file_path}: {e}")
                    results[file_path] = False
        return results

    def can_reference_previous_change(self, file_path: str) -> bool:
        """Check if we have previous changes for this file in session."""
        return file_path in self._file_snapshots


# =============================================================================
# v3.1: Checkpoint-Based Mid-Edit Error Recovery
# =============================================================================

@dataclass
class Checkpoint:
    """Represents a saved state checkpoint for error recovery."""
    id: str
    name: str
    created_at: float
    file_states: Dict[str, str]  # path -> content at checkpoint time
    metadata: Dict[str, Any] = field(default_factory=dict)
    parent_checkpoint: Optional[str] = None  # For nested checkpoints

    def __post_init__(self):
        if not self.metadata:
            self.metadata = {}


class CheckpointManager:
    """
    v3.1: Robust checkpoint-based error recovery for multi-file editing.

    Provides:
    - Named checkpoints with timestamps
    - Automatic pre-edit checkpointing
    - Nested checkpoints for complex operations
    - Atomic rollback to any checkpoint
    - Checkpoint persistence to disk for crash recovery
    - Cleanup of stale checkpoints

    Usage:
        manager = CheckpointManager()

        # Create checkpoint before risky operation
        cp_id = await manager.create_checkpoint("before_refactor", [file1, file2])

        try:
            # Perform edits...
            await edit_files()
        except Exception:
            # Rollback on error
            await manager.rollback_to_checkpoint(cp_id)
        finally:
            # Clean up checkpoint
            await manager.delete_checkpoint(cp_id)
    """

    DEFAULT_CHECKPOINT_DIR = ".jarvis_checkpoints"
    MAX_CHECKPOINTS = 50
    CHECKPOINT_TTL_HOURS = 24

    def __init__(
        self,
        checkpoint_dir: Optional[Path] = None,
        persist_to_disk: bool = True,
    ):
        """
        Initialize checkpoint manager.

        Args:
            checkpoint_dir: Directory to store checkpoint data. Defaults to .jarvis_checkpoints
            persist_to_disk: Whether to persist checkpoints to disk for crash recovery
        """
        self._checkpoint_dir = checkpoint_dir or Path(self.DEFAULT_CHECKPOINT_DIR)
        self._persist_to_disk = persist_to_disk
        self._checkpoints: Dict[str, Checkpoint] = {}
        self._checkpoint_stack: List[str] = []  # For nested checkpoints
        self._lock = asyncio.Lock()

        if self._persist_to_disk:
            self._checkpoint_dir.mkdir(parents=True, exist_ok=True)
            # Load existing checkpoints from disk
            asyncio.create_task(self._load_persisted_checkpoints())

    async def _load_persisted_checkpoints(self) -> None:
        """Load checkpoints from disk on startup."""
        if not self._persist_to_disk:
            return

        try:
            index_path = self._checkpoint_dir / "checkpoint_index.json"
            if index_path.exists():
                with open(index_path, 'r') as f:
                    index = json.load(f)

                for cp_id, cp_data in index.items():
                    # Load file states
                    file_states = {}
                    for file_path in cp_data.get("files", []):
                        state_file = self._checkpoint_dir / f"{cp_id}_{hashlib.md5(file_path.encode()).hexdigest()[:12]}.state"
                        if state_file.exists():
                            file_states[file_path] = state_file.read_text(encoding="utf-8")

                    self._checkpoints[cp_id] = Checkpoint(
                        id=cp_id,
                        name=cp_data.get("name", ""),
                        created_at=cp_data.get("created_at", 0),
                        file_states=file_states,
                        metadata=cp_data.get("metadata", {}),
                        parent_checkpoint=cp_data.get("parent_checkpoint"),
                    )

                logger.info(f"✅ Loaded {len(self._checkpoints)} checkpoints from disk")
        except Exception as e:
            logger.warning(f"Failed to load persisted checkpoints: {e}")

    async def _persist_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist a checkpoint to disk."""
        if not self._persist_to_disk:
            return

        try:
            # Save file states
            for file_path, content in checkpoint.file_states.items():
                state_file = self._checkpoint_dir / f"{checkpoint.id}_{hashlib.md5(file_path.encode()).hexdigest()[:12]}.state"
                state_file.write_text(content, encoding="utf-8")

            # Update index
            index_path = self._checkpoint_dir / "checkpoint_index.json"
            index = {}
            if index_path.exists():
                with open(index_path, 'r') as f:
                    index = json.load(f)

            index[checkpoint.id] = {
                "name": checkpoint.name,
                "created_at": checkpoint.created_at,
                "files": list(checkpoint.file_states.keys()),
                "metadata": checkpoint.metadata,
                "parent_checkpoint": checkpoint.parent_checkpoint,
            }

            with open(index_path, 'w') as f:
                json.dump(index, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to persist checkpoint {checkpoint.id}: {e}")

    async def create_checkpoint(
        self,
        name: str,
        file_paths: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a checkpoint saving the current state of specified files.

        Args:
            name: Human-readable name for the checkpoint
            file_paths: List of file paths to include in checkpoint
            metadata: Optional metadata to attach to checkpoint

        Returns:
            Checkpoint ID for later reference
        """
        async with self._lock:
            checkpoint_id = f"cp_{uuid.uuid4().hex[:12]}"

            # Read current content of all files
            file_states = {}
            for file_path in file_paths:
                try:
                    path = Path(file_path)
                    if path.exists():
                        file_states[file_path] = path.read_text(encoding="utf-8")
                    else:
                        # File doesn't exist yet - store empty string as marker
                        file_states[file_path] = ""
                except Exception as e:
                    logger.warning(f"Failed to read {file_path} for checkpoint: {e}")

            # Get parent checkpoint if we're in a nested operation
            parent_id = self._checkpoint_stack[-1] if self._checkpoint_stack else None

            checkpoint = Checkpoint(
                id=checkpoint_id,
                name=name,
                created_at=time.time(),
                file_states=file_states,
                metadata=metadata or {},
                parent_checkpoint=parent_id,
            )

            self._checkpoints[checkpoint_id] = checkpoint
            self._checkpoint_stack.append(checkpoint_id)

            # Persist to disk
            await self._persist_checkpoint(checkpoint)

            # Cleanup old checkpoints if needed
            await self._cleanup_old_checkpoints()

            logger.info(f"✅ Created checkpoint '{name}' ({checkpoint_id}) with {len(file_states)} files")
            return checkpoint_id

    async def rollback_to_checkpoint(
        self,
        checkpoint_id: str,
        delete_after_rollback: bool = True,
    ) -> Dict[str, bool]:
        """
        Rollback all files to their state at the given checkpoint.

        Args:
            checkpoint_id: ID of checkpoint to rollback to
            delete_after_rollback: Whether to delete the checkpoint after successful rollback

        Returns:
            Dict mapping file paths to success status
        """
        async with self._lock:
            checkpoint = self._checkpoints.get(checkpoint_id)
            if not checkpoint:
                logger.error(f"Checkpoint {checkpoint_id} not found")
                return {}

            results = {}
            for file_path, original_content in checkpoint.file_states.items():
                try:
                    path = Path(file_path)
                    if original_content == "":
                        # File didn't exist at checkpoint time - delete if it exists now
                        if path.exists():
                            path.unlink()
                            logger.info(f"Deleted {file_path} (didn't exist at checkpoint)")
                        results[file_path] = True
                    else:
                        # Restore original content
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(original_content, encoding="utf-8")
                        results[file_path] = True
                        logger.info(f"Restored {file_path} to checkpoint state")
                except Exception as e:
                    logger.error(f"Failed to rollback {file_path}: {e}")
                    results[file_path] = False

            if delete_after_rollback:
                await self._delete_checkpoint_internal(checkpoint_id)

            # Pop from stack if this was the most recent checkpoint
            if self._checkpoint_stack and self._checkpoint_stack[-1] == checkpoint_id:
                self._checkpoint_stack.pop()

            return results

    async def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        """Get checkpoint by ID."""
        async with self._lock:
            return self._checkpoints.get(checkpoint_id)

    async def list_checkpoints(self) -> List[Dict[str, Any]]:
        """List all checkpoints with summary info."""
        async with self._lock:
            return [
                {
                    "id": cp.id,
                    "name": cp.name,
                    "created_at": cp.created_at,
                    "file_count": len(cp.file_states),
                    "files": list(cp.file_states.keys()),
                    "metadata": cp.metadata,
                    "parent": cp.parent_checkpoint,
                    "age_seconds": time.time() - cp.created_at,
                }
                for cp in sorted(
                    self._checkpoints.values(),
                    key=lambda c: c.created_at,
                    reverse=True
                )
            ]

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        """Delete a checkpoint."""
        async with self._lock:
            return await self._delete_checkpoint_internal(checkpoint_id)

    async def _delete_checkpoint_internal(self, checkpoint_id: str) -> bool:
        """Internal method to delete checkpoint without acquiring lock."""
        checkpoint = self._checkpoints.pop(checkpoint_id, None)
        if not checkpoint:
            return False

        # Remove from stack
        if checkpoint_id in self._checkpoint_stack:
            self._checkpoint_stack.remove(checkpoint_id)

        # Delete persisted files
        if self._persist_to_disk:
            try:
                for file_path in checkpoint.file_states:
                    state_file = self._checkpoint_dir / f"{checkpoint_id}_{hashlib.md5(file_path.encode()).hexdigest()[:12]}.state"
                    if state_file.exists():
                        state_file.unlink()

                # Update index
                index_path = self._checkpoint_dir / "checkpoint_index.json"
                if index_path.exists():
                    with open(index_path, 'r') as f:
                        index = json.load(f)
                    index.pop(checkpoint_id, None)
                    with open(index_path, 'w') as f:
                        json.dump(index, f, indent=2)
            except Exception as e:
                logger.warning(f"Failed to delete persisted checkpoint files: {e}")

        return True

    async def _cleanup_old_checkpoints(self) -> int:
        """Remove checkpoints older than TTL or exceeding max count."""
        cleaned = 0
        cutoff = time.time() - (self.CHECKPOINT_TTL_HOURS * 3600)

        # Sort by creation time (oldest first)
        sorted_checkpoints = sorted(
            self._checkpoints.values(),
            key=lambda c: c.created_at
        )

        # Remove old checkpoints
        for cp in sorted_checkpoints:
            if cp.created_at < cutoff:
                await self._delete_checkpoint_internal(cp.id)
                cleaned += 1

        # Remove excess checkpoints
        while len(self._checkpoints) > self.MAX_CHECKPOINTS:
            oldest = min(self._checkpoints.values(), key=lambda c: c.created_at)
            await self._delete_checkpoint_internal(oldest.id)
            cleaned += 1

        if cleaned:
            logger.info(f"Cleaned up {cleaned} old checkpoints")

        return cleaned

    async def get_current_checkpoint_id(self) -> Optional[str]:
        """Get the ID of the current (most recent) checkpoint in the stack."""
        async with self._lock:
            return self._checkpoint_stack[-1] if self._checkpoint_stack else None

    async def pop_checkpoint(self) -> Optional[str]:
        """Pop and return the most recent checkpoint from the stack."""
        async with self._lock:
            if self._checkpoint_stack:
                return self._checkpoint_stack.pop()
            return None

    async def commit_checkpoint(self, checkpoint_id: str) -> bool:
        """
        Commit a checkpoint (mark edits as successful, delete checkpoint).

        Use this when edits complete successfully and you no longer need the
        rollback capability.
        """
        return await self.delete_checkpoint(checkpoint_id)

    @asynccontextmanager
    async def checkpoint_context(
        self,
        name: str,
        file_paths: List[str],
        metadata: Optional[Dict[str, Any]] = None,
        auto_rollback_on_error: bool = True,
    ):
        """
        Context manager for automatic checkpoint management.

        Usage:
            async with checkpoint_manager.checkpoint_context(
                "refactoring_foo",
                ["file1.py", "file2.py"]
            ) as cp_id:
                # Perform edits...
                # If an exception is raised, files are automatically rolled back

        Args:
            name: Checkpoint name
            file_paths: Files to checkpoint
            metadata: Optional metadata
            auto_rollback_on_error: Whether to automatically rollback on exception
        """
        checkpoint_id = await self.create_checkpoint(name, file_paths, metadata)
        try:
            yield checkpoint_id
            # Success - commit the checkpoint
            await self.commit_checkpoint(checkpoint_id)
        except Exception as e:
            if auto_rollback_on_error:
                logger.warning(f"Error during checkpoint '{name}', rolling back: {e}")
                await self.rollback_to_checkpoint(checkpoint_id)
            else:
                # Just delete checkpoint without rollback
                await self.delete_checkpoint(checkpoint_id)
            raise

    async def get_file_diff_from_checkpoint(
        self,
        checkpoint_id: str,
        file_path: str,
    ) -> Optional[str]:
        """
        Get diff between checkpoint state and current file state.

        Returns:
            Unified diff string, or None if file not in checkpoint
        """
        async with self._lock:
            checkpoint = self._checkpoints.get(checkpoint_id)
            if not checkpoint or file_path not in checkpoint.file_states:
                return None

            original = checkpoint.file_states[file_path]

            try:
                current = Path(file_path).read_text(encoding="utf-8")
            except Exception:
                current = ""

            import difflib
            diff = difflib.unified_diff(
                original.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"checkpoint/{file_path}",
                tofile=f"current/{file_path}",
            )
            return "".join(diff)


# Global checkpoint manager instance
_checkpoint_manager: Optional[CheckpointManager] = None


def get_checkpoint_manager() -> CheckpointManager:
    """Get or create the global checkpoint manager."""
    global _checkpoint_manager
    if _checkpoint_manager is None:
        _checkpoint_manager = CheckpointManager()
    return _checkpoint_manager


# =============================================================================
# v3.1: Smart File Chunking for Large Files
# =============================================================================

class ChunkBoundaryType(Enum):
    """Types of logical boundaries for chunking."""
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    MODULE_SECTION = "module_section"
    IMPORT_BLOCK = "import_block"
    DOCSTRING = "docstring"
    COMMENT_BLOCK = "comment_block"
    ARBITRARY = "arbitrary"  # Fallback when no logical boundary found


@dataclass
class FileChunk:
    """Represents a chunk of a large file."""
    id: str
    file_path: str
    start_line: int
    end_line: int
    content: str
    boundary_type: ChunkBoundaryType
    entity_name: Optional[str] = None  # e.g., class name, function name
    dependencies: List[str] = field(default_factory=list)  # IDs of dependent chunks
    context_before: str = ""  # Lines needed for context
    context_after: str = ""

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    @property
    def token_estimate(self) -> int:
        """Rough estimate of tokens (4 chars per token)."""
        return len(self.content) // 4


@dataclass
class ChunkedFile:
    """A file that has been split into chunks."""
    file_path: str
    total_lines: int
    total_chunks: int
    chunks: List[FileChunk]
    chunk_order: List[str]  # IDs in order
    dependency_graph: Dict[str, List[str]]  # chunk_id -> list of dependent chunk_ids
    created_at: float = field(default_factory=time.time)

    def get_chunk_by_id(self, chunk_id: str) -> Optional[FileChunk]:
        """Get a chunk by ID."""
        for chunk in self.chunks:
            if chunk.id == chunk_id:
                return chunk
        return None

    def get_chunks_for_entity(self, entity_name: str) -> List[FileChunk]:
        """Get all chunks that belong to a named entity."""
        return [c for c in self.chunks if c.entity_name == entity_name]


class SmartFileChunker:
    """
    v3.1: Intelligent file chunking for large files that exceed context limits.

    Features:
    - AST-based parsing for Python files to find logical boundaries
    - Fallback to line-based chunking with overlap for other languages
    - Dependency tracking between chunks (e.g., methods need class context)
    - Configurable chunk sizes with soft/hard limits
    - Context preservation across chunk boundaries
    - Efficient reassembly after processing

    Usage:
        chunker = SmartFileChunker(max_chunk_tokens=4000)
        chunked = await chunker.chunk_file("large_file.py")

        for chunk in chunked.chunks:
            # Process each chunk...
            improved = await improve_chunk(chunk)

        # Reassemble
        final_content = await chunker.reassemble_chunks(chunked, modified_chunks)
    """

    DEFAULT_MAX_CHUNK_TOKENS = 4000  # ~16K characters
    DEFAULT_OVERLAP_LINES = 10  # Context overlap between chunks
    DEFAULT_CONTEXT_LINES = 5  # Lines of context before/after

    def __init__(
        self,
        max_chunk_tokens: int = DEFAULT_MAX_CHUNK_TOKENS,
        overlap_lines: int = DEFAULT_OVERLAP_LINES,
        context_lines: int = DEFAULT_CONTEXT_LINES,
    ):
        """
        Initialize chunker.

        Args:
            max_chunk_tokens: Maximum tokens per chunk (soft limit)
            overlap_lines: Lines of overlap between consecutive chunks
            context_lines: Lines of context to include before/after chunks
        """
        self._max_tokens = max_chunk_tokens
        self._overlap_lines = overlap_lines
        self._context_lines = context_lines

    def should_chunk(self, content: str) -> bool:
        """Check if a file needs to be chunked."""
        estimated_tokens = len(content) // 4
        return estimated_tokens > self._max_tokens

    async def chunk_file(
        self,
        file_path: str,
        content: Optional[str] = None,
    ) -> ChunkedFile:
        """
        Chunk a file into logical segments.

        Args:
            file_path: Path to the file
            content: Optional file content (reads from disk if not provided)

        Returns:
            ChunkedFile with chunks and metadata
        """
        if content is None:
            content = Path(file_path).read_text(encoding="utf-8")

        lines = content.splitlines(keepends=True)
        total_lines = len(lines)

        # Detect file type and use appropriate chunking strategy
        if file_path.endswith('.py'):
            chunks = await self._chunk_python_file(file_path, content, lines)
        else:
            # Fallback to line-based chunking
            chunks = await self._chunk_by_lines(file_path, content, lines)

        # Build dependency graph
        dependency_graph = self._build_dependency_graph(chunks)

        return ChunkedFile(
            file_path=file_path,
            total_lines=total_lines,
            total_chunks=len(chunks),
            chunks=chunks,
            chunk_order=[c.id for c in chunks],
            dependency_graph=dependency_graph,
        )

    async def _chunk_python_file(
        self,
        file_path: str,
        content: str,
        lines: List[str],
    ) -> List[FileChunk]:
        """Chunk a Python file using AST for logical boundaries."""
        chunks = []

        try:
            import ast
            tree = ast.parse(content)
        except SyntaxError:
            # Fallback to line-based if parsing fails
            return await self._chunk_by_lines(file_path, content, lines)

        # Extract top-level entities
        entities = []

        # First, handle imports (usually at the top)
        import_end = 0
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                import_end = max(import_end, node.end_lineno or 0)
            else:
                break

        if import_end > 0:
            entities.append({
                "type": ChunkBoundaryType.IMPORT_BLOCK,
                "name": "__imports__",
                "start": 1,
                "end": import_end,
            })

        # Extract classes and functions
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                entities.append({
                    "type": ChunkBoundaryType.CLASS,
                    "name": node.name,
                    "start": node.lineno,
                    "end": node.end_lineno or node.lineno,
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                entities.append({
                    "type": ChunkBoundaryType.FUNCTION,
                    "name": node.name,
                    "start": node.lineno,
                    "end": node.end_lineno or node.lineno,
                })

        # Sort by start line
        entities.sort(key=lambda e: e["start"])

        # Create chunks from entities
        current_line = 1
        chunk_idx = 0

        for entity in entities:
            # Handle gap before this entity
            if entity["start"] > current_line:
                gap_start = current_line
                gap_end = entity["start"] - 1
                gap_content = "".join(lines[gap_start - 1:gap_end])

                if gap_content.strip():  # Only create chunk if non-empty
                    chunks.append(FileChunk(
                        id=f"chunk_{chunk_idx}_{hashlib.md5(gap_content.encode()).hexdigest()[:8]}",
                        file_path=file_path,
                        start_line=gap_start,
                        end_line=gap_end,
                        content=gap_content,
                        boundary_type=ChunkBoundaryType.MODULE_SECTION,
                        entity_name=None,
                    ))
                    chunk_idx += 1

            # Create chunk for this entity
            entity_content = "".join(lines[entity["start"] - 1:entity["end"]])

            # Check if entity is too large and needs sub-chunking
            if len(entity_content) // 4 > self._max_tokens:
                sub_chunks = await self._chunk_large_entity(
                    file_path, entity, lines, chunk_idx
                )
                chunks.extend(sub_chunks)
                chunk_idx += len(sub_chunks)
            else:
                chunks.append(FileChunk(
                    id=f"chunk_{chunk_idx}_{entity['name']}",
                    file_path=file_path,
                    start_line=entity["start"],
                    end_line=entity["end"],
                    content=entity_content,
                    boundary_type=entity["type"],
                    entity_name=entity["name"],
                ))
                chunk_idx += 1

            current_line = entity["end"] + 1

        # Handle remaining content after last entity
        if current_line <= len(lines):
            remaining = "".join(lines[current_line - 1:])
            if remaining.strip():
                chunks.append(FileChunk(
                    id=f"chunk_{chunk_idx}_tail",
                    file_path=file_path,
                    start_line=current_line,
                    end_line=len(lines),
                    content=remaining,
                    boundary_type=ChunkBoundaryType.MODULE_SECTION,
                ))

        # Add context to chunks
        self._add_context_to_chunks(chunks, lines)

        return chunks

    async def _chunk_large_entity(
        self,
        file_path: str,
        entity: Dict[str, Any],
        lines: List[str],
        base_idx: int,
    ) -> List[FileChunk]:
        """Sub-chunk a large entity (class or function) by methods."""
        chunks = []
        entity_lines = lines[entity["start"] - 1:entity["end"]]
        entity_content = "".join(entity_lines)

        if entity["type"] == ChunkBoundaryType.CLASS:
            # Parse class to extract methods
            try:
                import ast
                # Add minimal context for parsing
                tree = ast.parse(entity_content)
                class_node = tree.body[0]

                # Get class header (decorators + class def line + docstring)
                header_end = class_node.lineno
                if class_node.body:
                    first_body = class_node.body[0]
                    if isinstance(first_body, ast.Expr) and isinstance(first_body.value, ast.Constant):
                        # Has docstring
                        header_end = first_body.end_lineno or first_body.lineno

                # Create header chunk
                header_content = "".join(entity_lines[:header_end])
                chunks.append(FileChunk(
                    id=f"chunk_{base_idx}_{entity['name']}_header",
                    file_path=file_path,
                    start_line=entity["start"],
                    end_line=entity["start"] + header_end - 1,
                    content=header_content,
                    boundary_type=ChunkBoundaryType.CLASS,
                    entity_name=f"{entity['name']}.__header__",
                ))

                # Create chunks for each method
                method_idx = 0
                for node in class_node.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_start = node.lineno - 1  # Adjust for 0-indexed
                        method_end = (node.end_lineno or node.lineno) - 1
                        method_content = "".join(entity_lines[method_start:method_end + 1])

                        chunks.append(FileChunk(
                            id=f"chunk_{base_idx + method_idx + 1}_{entity['name']}.{node.name}",
                            file_path=file_path,
                            start_line=entity["start"] + method_start,
                            end_line=entity["start"] + method_end,
                            content=method_content,
                            boundary_type=ChunkBoundaryType.METHOD,
                            entity_name=f"{entity['name']}.{node.name}",
                            dependencies=[chunks[0].id],  # Depends on class header
                        ))
                        method_idx += 1

                return chunks
            except Exception:
                pass  # Fall through to line-based

        # Fallback: split by lines
        return await self._chunk_entity_by_lines(file_path, entity, lines, base_idx)

    async def _chunk_entity_by_lines(
        self,
        file_path: str,
        entity: Dict[str, Any],
        lines: List[str],
        base_idx: int,
    ) -> List[FileChunk]:
        """Split a large entity by lines with overlap."""
        chunks = []
        entity_lines = lines[entity["start"] - 1:entity["end"]]
        total_lines = len(entity_lines)

        # Calculate lines per chunk
        max_lines = self._max_tokens // 20  # Rough estimate: 20 chars per line

        current_start = 0
        chunk_idx = 0

        while current_start < total_lines:
            chunk_end = min(current_start + max_lines, total_lines)
            chunk_content = "".join(entity_lines[current_start:chunk_end])

            chunks.append(FileChunk(
                id=f"chunk_{base_idx + chunk_idx}_{entity['name']}_part{chunk_idx}",
                file_path=file_path,
                start_line=entity["start"] + current_start,
                end_line=entity["start"] + chunk_end - 1,
                content=chunk_content,
                boundary_type=ChunkBoundaryType.ARBITRARY,
                entity_name=f"{entity['name']}_part{chunk_idx}",
                dependencies=[chunks[-1].id] if chunks else [],
            ))

            current_start = chunk_end - self._overlap_lines
            chunk_idx += 1

        return chunks

    async def _chunk_by_lines(
        self,
        file_path: str,
        content: str,
        lines: List[str],
    ) -> List[FileChunk]:
        """Fallback line-based chunking with overlap."""
        chunks = []
        total_lines = len(lines)
        max_lines = self._max_tokens // 20  # Rough estimate

        current_start = 0
        chunk_idx = 0

        while current_start < total_lines:
            chunk_end = min(current_start + max_lines, total_lines)
            chunk_content = "".join(lines[current_start:chunk_end])

            chunks.append(FileChunk(
                id=f"chunk_{chunk_idx}",
                file_path=file_path,
                start_line=current_start + 1,
                end_line=chunk_end,
                content=chunk_content,
                boundary_type=ChunkBoundaryType.ARBITRARY,
            ))

            current_start = chunk_end - self._overlap_lines
            chunk_idx += 1

        self._add_context_to_chunks(chunks, lines)
        return chunks

    def _add_context_to_chunks(
        self,
        chunks: List[FileChunk],
        lines: List[str],
    ) -> None:
        """Add context lines before/after each chunk."""
        for i, chunk in enumerate(chunks):
            # Context before
            ctx_start = max(0, chunk.start_line - 1 - self._context_lines)
            ctx_end = chunk.start_line - 1
            if ctx_end > ctx_start:
                chunk.context_before = "".join(lines[ctx_start:ctx_end])

            # Context after
            ctx_start = chunk.end_line
            ctx_end = min(len(lines), chunk.end_line + self._context_lines)
            if ctx_end > ctx_start:
                chunk.context_after = "".join(lines[ctx_start:ctx_end])

    def _build_dependency_graph(
        self,
        chunks: List[FileChunk],
    ) -> Dict[str, List[str]]:
        """Build dependency graph between chunks."""
        graph: Dict[str, List[str]] = {}

        for chunk in chunks:
            graph[chunk.id] = chunk.dependencies.copy()

            # Classes depend on imports
            if chunk.boundary_type == ChunkBoundaryType.CLASS:
                for other in chunks:
                    if other.boundary_type == ChunkBoundaryType.IMPORT_BLOCK:
                        if other.id not in graph[chunk.id]:
                            graph[chunk.id].append(other.id)

            # Functions might depend on other functions (basic heuristic)
            if chunk.boundary_type == ChunkBoundaryType.FUNCTION:
                for other in chunks:
                    if other.boundary_type == ChunkBoundaryType.IMPORT_BLOCK:
                        if other.id not in graph[chunk.id]:
                            graph[chunk.id].append(other.id)

        return graph

    async def reassemble_chunks(
        self,
        chunked_file: ChunkedFile,
        modified_chunks: Dict[str, str],  # chunk_id -> new content
    ) -> str:
        """
        Reassemble a chunked file with modifications.

        Args:
            chunked_file: The original chunked file
            modified_chunks: Dict mapping chunk IDs to their new content

        Returns:
            Reassembled file content
        """
        # Build final content by going through chunks in order
        result_lines: List[str] = []
        last_end_line = 0

        for chunk_id in chunked_file.chunk_order:
            chunk = chunked_file.get_chunk_by_id(chunk_id)
            if not chunk:
                continue

            # Use modified content if available, otherwise original
            content = modified_chunks.get(chunk_id, chunk.content)

            # Handle gaps (shouldn't happen if chunking was correct)
            if chunk.start_line > last_end_line + 1:
                # There's a gap - this shouldn't happen normally
                pass

            # Add the chunk content
            result_lines.append(content)
            last_end_line = chunk.end_line

        return "".join(result_lines)

    async def get_chunk_for_line(
        self,
        chunked_file: ChunkedFile,
        line_number: int,
    ) -> Optional[FileChunk]:
        """Get the chunk containing a specific line number."""
        for chunk in chunked_file.chunks:
            if chunk.start_line <= line_number <= chunk.end_line:
                return chunk
        return None

    async def get_chunks_with_dependencies(
        self,
        chunked_file: ChunkedFile,
        chunk_id: str,
    ) -> List[FileChunk]:
        """Get a chunk and all its dependencies (for providing full context)."""
        result = []
        visited = set()

        def collect_deps(cid: str):
            if cid in visited:
                return
            visited.add(cid)

            chunk = chunked_file.get_chunk_by_id(cid)
            if chunk:
                # First collect dependencies
                for dep_id in chunked_file.dependency_graph.get(cid, []):
                    collect_deps(dep_id)
                result.append(chunk)

        collect_deps(chunk_id)
        return result

    def estimate_chunks_needed(self, content: str) -> int:
        """Estimate how many chunks a file will need."""
        tokens = len(content) // 4
        return max(1, tokens // self._max_tokens + 1)


# Global chunker instance
_smart_chunker: Optional[SmartFileChunker] = None


def get_smart_chunker(
    max_tokens: int = SmartFileChunker.DEFAULT_MAX_CHUNK_TOKENS
) -> SmartFileChunker:
    """Get or create the global smart chunker."""
    global _smart_chunker
    if _smart_chunker is None or _smart_chunker._max_tokens != max_tokens:
        _smart_chunker = SmartFileChunker(max_chunk_tokens=max_tokens)
    return _smart_chunker


# =============================================================================
# v3.1: ADVANCED SELF-PROGRAMMING FEATURES
# =============================================================================

class RefactoringType(Enum):
    """Types of refactoring operations."""
    RENAME_SYMBOL = "rename_symbol"
    EXTRACT_METHOD = "extract_method"
    EXTRACT_CLASS = "extract_class"
    INLINE_VARIABLE = "inline_variable"
    MOVE_FUNCTION = "move_function"
    CHANGE_SIGNATURE = "change_signature"


@dataclass
class SymbolReference:
    """Represents a reference to a symbol in code."""
    file_path: str
    line: int
    column: int
    end_column: int
    symbol_name: str
    context: str  # Line content for verification
    is_definition: bool = False
    is_import: bool = False


@dataclass
class RefactoringPlan:
    """Plan for a refactoring operation."""
    id: str
    refactoring_type: RefactoringType
    target_symbol: str
    new_value: Optional[str]  # New name for rename, etc.
    affected_files: List[str]
    references: List[SymbolReference]
    estimated_changes: int
    risk_score: float
    created_at: float = field(default_factory=time.time)


@dataclass
class DependencyInfo:
    """Information about a Python dependency."""
    name: str
    import_name: str  # e.g., 'cv2' for 'opencv-python'
    version_spec: Optional[str] = None
    is_stdlib: bool = False
    is_local: bool = False
    first_seen_file: Optional[str] = None


class AdvancedRefactoringEngine:
    """
    v3.1: Enterprise-grade refactoring engine with cross-file symbol awareness.

    Provides:
    - Cross-file symbol renaming using LSP references
    - Import statement auto-updates when files move
    - Extract method/class refactorings
    - Inline variable transformations
    - Safe atomic multi-file changes with rollback

    Uses The Watcher's LSP integration for accurate symbol resolution
    (no regex guessing - compiler-level accuracy).
    """

    # Known import name -> package name mappings
    IMPORT_TO_PACKAGE = {
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "sklearn": "scikit-learn",
        "yaml": "PyYAML",
        "bs4": "beautifulsoup4",
        "dotenv": "python-dotenv",
        "jwt": "PyJWT",
        "dateutil": "python-dateutil",
        "magic": "python-magic",
        "gi": "PyGObject",
    }

    def __init__(self, watcher: Optional[Any] = None):
        """
        Initialize refactoring engine.

        Args:
            watcher: The Watcher LSP integration instance
        """
        self._watcher = watcher
        self._lock = asyncio.Lock()
        self._pending_plans: Dict[str, RefactoringPlan] = {}
        self._checkpoint_manager = get_checkpoint_manager()

    async def set_watcher(self, watcher: Any) -> None:
        """Set The Watcher instance for LSP operations."""
        self._watcher = watcher

    async def rename_symbol(
        self,
        file_path: str,
        line: int,
        column: int,
        new_name: str,
        dry_run: bool = True,
    ) -> RefactoringPlan:
        """
        Rename a symbol across all files that reference it.

        This is THE killer feature - rename a function/class and it updates
        every single usage across the entire codebase.

        Args:
            file_path: File containing the symbol definition
            line: Line number of the symbol (1-indexed)
            column: Column number of the symbol (1-indexed)
            new_name: New name for the symbol
            dry_run: If True, only plan the refactoring without executing

        Returns:
            RefactoringPlan with all affected locations
        """
        if not self._watcher:
            raise RuntimeError("The Watcher not available for symbol resolution")

        async with self._lock:
            # Get the symbol at this location
            symbol_info = await self._get_symbol_at_location(file_path, line, column)
            if not symbol_info:
                raise ValueError(f"No symbol found at {file_path}:{line}:{column}")

            old_name = symbol_info["name"]

            # Get all references to this symbol across the workspace
            references = await self._get_all_references(
                file_path, line, column, old_name
            )

            if not references:
                raise ValueError(f"No references found for symbol '{old_name}'")

            # Build the refactoring plan
            affected_files = list(set(ref.file_path for ref in references))
            plan = RefactoringPlan(
                id=f"rename_{uuid.uuid4().hex[:12]}",
                refactoring_type=RefactoringType.RENAME_SYMBOL,
                target_symbol=old_name,
                new_value=new_name,
                affected_files=affected_files,
                references=references,
                estimated_changes=len(references),
                risk_score=self._calculate_risk_score(references),
            )

            self._pending_plans[plan.id] = plan

            if not dry_run:
                await self._execute_rename(plan)

            return plan

    async def _get_symbol_at_location(
        self,
        file_path: str,
        line: int,
        column: int,
    ) -> Optional[Dict[str, Any]]:
        """Get symbol information at a specific location."""
        try:
            # Read the file to get the symbol name
            content = Path(file_path).read_text(encoding="utf-8")
            lines = content.splitlines()

            if line > len(lines):
                return None

            line_content = lines[line - 1]

            # Find word at column
            word_start = column - 1
            word_end = column - 1

            while word_start > 0 and line_content[word_start - 1].isalnum() or line_content[word_start - 1] == '_':
                word_start -= 1

            while word_end < len(line_content) and (line_content[word_end].isalnum() or line_content[word_end] == '_'):
                word_end += 1

            symbol_name = line_content[word_start:word_end]

            if not symbol_name:
                return None

            return {
                "name": symbol_name,
                "line": line,
                "column": column,
                "start_column": word_start + 1,
                "end_column": word_end + 1,
            }

        except Exception as e:
            logger.error(f"Failed to get symbol at location: {e}")
            return None

    async def _get_all_references(
        self,
        file_path: str,
        line: int,
        column: int,
        symbol_name: str,
    ) -> List[SymbolReference]:
        """Get all references to a symbol using The Watcher's LSP."""
        references = []

        try:
            if self._watcher and hasattr(self._watcher, 'get_references'):
                # Use LSP to get accurate references
                lsp_refs = await self._watcher.get_references(
                    Path(file_path), line, column, include_declaration=True
                )

                for ref in lsp_refs:
                    ref_path = self._uri_to_path(ref.uri) if hasattr(ref, 'uri') else str(ref.get('uri', ''))
                    ref_line = ref.range.start.line + 1 if hasattr(ref, 'range') else ref.get('range', {}).get('start', {}).get('line', 0) + 1
                    ref_col = ref.range.start.character + 1 if hasattr(ref, 'range') else ref.get('range', {}).get('start', {}).get('character', 0) + 1
                    ref_end_col = ref.range.end.character + 1 if hasattr(ref, 'range') else ref.get('range', {}).get('end', {}).get('character', 0) + 1

                    # Get context
                    try:
                        content = Path(ref_path).read_text(encoding="utf-8")
                        context = content.splitlines()[ref_line - 1] if ref_line <= len(content.splitlines()) else ""
                    except Exception:
                        context = ""

                    references.append(SymbolReference(
                        file_path=ref_path,
                        line=ref_line,
                        column=ref_col,
                        end_column=ref_end_col,
                        symbol_name=symbol_name,
                        context=context,
                        is_definition=(ref_path == file_path and ref_line == line),
                        is_import="import " in context or "from " in context,
                    ))
            else:
                # Fallback to AST-based reference finding
                references = await self._find_references_with_ast(
                    file_path, symbol_name
                )

        except Exception as e:
            logger.error(f"Failed to get references: {e}")
            # Fallback
            references = await self._find_references_with_ast(file_path, symbol_name)

        return references

    async def _find_references_with_ast(
        self,
        definition_file: str,
        symbol_name: str,
    ) -> List[SymbolReference]:
        """Fallback: Find references using AST parsing."""
        import ast
        references = []

        # Get workspace root
        workspace = Path(definition_file).parent
        while workspace.parent != workspace:
            if (workspace / ".git").exists() or (workspace / "pyproject.toml").exists():
                break
            workspace = workspace.parent

        # Scan all Python files
        for py_file in workspace.rglob("*.py"):
            if "__pycache__" in str(py_file) or ".venv" in str(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, ast.Name) and node.id == symbol_name:
                        lines = content.splitlines()
                        line_content = lines[node.lineno - 1] if node.lineno <= len(lines) else ""

                        references.append(SymbolReference(
                            file_path=str(py_file),
                            line=node.lineno,
                            column=node.col_offset + 1,
                            end_column=node.col_offset + len(symbol_name) + 1,
                            symbol_name=symbol_name,
                            context=line_content,
                            is_definition=str(py_file) == definition_file,
                            is_import="import " in line_content,
                        ))

            except Exception:
                continue

        return references

    def _uri_to_path(self, uri: str) -> str:
        """Convert file:// URI to path."""
        if uri.startswith("file://"):
            return uri[7:]
        return uri

    def _calculate_risk_score(self, references: List[SymbolReference]) -> float:
        """Calculate risk score for a refactoring operation."""
        if not references:
            return 0.0

        # Factors that increase risk
        file_count = len(set(ref.file_path for ref in references))
        import_count = sum(1 for ref in references if ref.is_import)

        # More files = higher risk
        file_risk = min(file_count / 10, 1.0) * 0.4

        # More imports = higher risk (breaking imports is bad)
        import_risk = min(import_count / 5, 1.0) * 0.4

        # Large number of references = higher risk
        ref_risk = min(len(references) / 50, 1.0) * 0.2

        return file_risk + import_risk + ref_risk

    async def _execute_rename(self, plan: RefactoringPlan) -> Dict[str, bool]:
        """Execute a rename refactoring plan."""
        results = {}

        # Create checkpoint before making changes
        async with self._checkpoint_manager.checkpoint_context(
            f"rename_{plan.target_symbol}_to_{plan.new_value}",
            plan.affected_files,
        ) as checkpoint_id:

            # Group references by file
            refs_by_file: Dict[str, List[SymbolReference]] = {}
            for ref in plan.references:
                if ref.file_path not in refs_by_file:
                    refs_by_file[ref.file_path] = []
                refs_by_file[ref.file_path].append(ref)

            # Process each file
            for file_path, refs in refs_by_file.items():
                try:
                    content = Path(file_path).read_text(encoding="utf-8")
                    lines = content.splitlines(keepends=True)

                    # Sort refs by line (descending) to avoid offset issues
                    refs.sort(key=lambda r: (r.line, r.column), reverse=True)

                    for ref in refs:
                        line_idx = ref.line - 1
                        if line_idx < len(lines):
                            line = lines[line_idx]
                            # Replace the symbol
                            before = line[:ref.column - 1]
                            after = line[ref.end_column - 1:]
                            lines[line_idx] = before + plan.new_value + after

                    # Write updated content
                    new_content = "".join(lines)
                    Path(file_path).write_text(new_content, encoding="utf-8")
                    results[file_path] = True
                    logger.info(f"Updated {len(refs)} references in {file_path}")

                except Exception as e:
                    logger.error(f"Failed to update {file_path}: {e}")
                    results[file_path] = False
                    raise  # Trigger rollback

        return results

    async def update_imports_for_move(
        self,
        old_path: str,
        new_path: str,
        workspace_root: Optional[str] = None,
    ) -> Dict[str, bool]:
        """
        Update all import statements when a file is moved/renamed.

        This automatically fixes broken imports after file operations.

        Args:
            old_path: Original file path
            new_path: New file path
            workspace_root: Root of the workspace to scan

        Returns:
            Dict mapping file paths to update success status
        """
        results = {}

        if not workspace_root:
            workspace_root = self._find_workspace_root(old_path)

        # Calculate old and new module paths
        old_module = self._path_to_module(old_path, workspace_root)
        new_module = self._path_to_module(new_path, workspace_root)

        if not old_module or not new_module:
            logger.warning("Could not determine module paths for import update")
            return results

        # Find all files that import from the old module
        workspace = Path(workspace_root)
        affected_files = []

        for py_file in workspace.rglob("*.py"):
            if "__pycache__" in str(py_file) or ".venv" in str(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
                if old_module in content:
                    affected_files.append(str(py_file))
            except Exception:
                continue

        if not affected_files:
            logger.info(f"No files import from {old_module}")
            return results

        # Create checkpoint
        async with self._checkpoint_manager.checkpoint_context(
            f"import_update_{old_module}",
            affected_files,
        ):
            for file_path in affected_files:
                try:
                    content = Path(file_path).read_text(encoding="utf-8")

                    # Update import statements
                    new_content = self._update_import_statements(
                        content, old_module, new_module
                    )

                    if new_content != content:
                        Path(file_path).write_text(new_content, encoding="utf-8")
                        results[file_path] = True
                        logger.info(f"Updated imports in {file_path}")
                    else:
                        results[file_path] = True  # No changes needed

                except Exception as e:
                    logger.error(f"Failed to update imports in {file_path}: {e}")
                    results[file_path] = False
                    raise

        return results

    def _find_workspace_root(self, file_path: str) -> str:
        """Find the workspace root directory."""
        path = Path(file_path).parent
        while path.parent != path:
            if (path / ".git").exists() or (path / "pyproject.toml").exists():
                return str(path)
            path = path.parent
        return str(Path(file_path).parent)

    def _path_to_module(self, file_path: str, workspace_root: str) -> Optional[str]:
        """Convert a file path to a Python module path."""
        try:
            rel_path = Path(file_path).relative_to(workspace_root)
            # Remove .py extension and convert to module path
            module_path = str(rel_path).replace("/", ".").replace("\\", ".")
            if module_path.endswith(".py"):
                module_path = module_path[:-3]
            return module_path
        except ValueError:
            return None

    def _update_import_statements(
        self,
        content: str,
        old_module: str,
        new_module: str,
    ) -> str:
        """Update import statements in file content."""
        import re

        # Handle different import patterns
        patterns = [
            # from old_module import ...
            (rf'from\s+{re.escape(old_module)}\s+import',
             f'from {new_module} import'),
            # import old_module
            (rf'import\s+{re.escape(old_module)}(\s|$|,)',
             f'import {new_module}\\1'),
            # from old_module.submodule import ...
            (rf'from\s+{re.escape(old_module)}\.(\S+)\s+import',
             f'from {new_module}.\\1 import'),
        ]

        for pattern, replacement in patterns:
            content = re.sub(pattern, replacement, content)

        return content

    async def extract_method(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        new_method_name: str,
    ) -> Optional[str]:
        """
        Extract lines into a new method.

        Args:
            file_path: Path to the file
            start_line: Start line of code to extract (1-indexed)
            end_line: End line of code to extract (1-indexed)
            new_method_name: Name for the new method

        Returns:
            Modified file content, or None on failure
        """
        try:
            content = Path(file_path).read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)

            # Get the lines to extract
            extracted = lines[start_line - 1:end_line]
            extracted_content = "".join(extracted)

            # Analyze the extracted code
            import ast
            try:
                tree = ast.parse(extracted_content)
            except SyntaxError:
                # Wrap in function body and try again
                indented = "\n".join("    " + line.rstrip() for line in extracted_content.splitlines())
                wrapped = f"def _temp():\n{indented}\n"
                tree = ast.parse(wrapped).body[0]

            # Find used variables (potential parameters)
            used_names = set()
            assigned_names = set()

            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    if isinstance(node.ctx, ast.Load):
                        used_names.add(node.id)
                    elif isinstance(node.ctx, ast.Store):
                        assigned_names.add(node.id)

            # Parameters are used but not assigned within the extracted code
            parameters = list(used_names - assigned_names - {'self', 'cls'})
            parameters.sort()

            # Determine indentation
            first_line = extracted[0] if extracted else ""
            base_indent = len(first_line) - len(first_line.lstrip())
            indent_str = first_line[:base_indent]

            # Build the new method
            param_str = ", ".join(parameters)
            method_lines = [f"{indent_str}def {new_method_name}(self, {param_str}):\n"]

            for line in extracted:
                # Add extra indentation for method body
                method_lines.append("    " + line if line.strip() else line)

            method_content = "".join(method_lines)

            # Build the call to the new method
            call_line = f"{indent_str}self.{new_method_name}({', '.join(parameters)})\n"

            # Replace extracted lines with the call
            new_lines = lines[:start_line - 1] + [call_line] + lines[end_line:]

            # Insert the new method (find a good location)
            # For now, insert right before the extraction point
            new_lines = new_lines[:start_line - 1] + [method_content, "\n"] + new_lines[start_line - 1:]

            return "".join(new_lines)

        except Exception as e:
            logger.error(f"Extract method failed: {e}")
            return None


class DependencyTracker:
    """
    v3.1: Intelligent dependency tracking and requirements.txt management.

    Features:
    - Auto-detect new imports in code
    - Update requirements.txt automatically
    - Distinguish stdlib from third-party
    - Version pinning based on installed packages
    - Import name to package name resolution
    """

    # Python stdlib modules (3.10+)
    STDLIB_MODULES = frozenset({
        "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio",
        "asyncore", "atexit", "audioop", "base64", "bdb", "binascii",
        "binhex", "bisect", "builtins", "bz2", "calendar", "cgi", "cgitb",
        "chunk", "cmath", "cmd", "code", "codecs", "codeop", "collections",
        "colorsys", "compileall", "concurrent", "configparser", "contextlib",
        "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
        "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal",
        "difflib", "dis", "distutils", "doctest", "email", "encodings",
        "enum", "errno", "faulthandler", "fcntl", "filecmp", "fileinput",
        "fnmatch", "fractions", "ftplib", "functools", "gc", "getopt",
        "getpass", "gettext", "glob", "graphlib", "grp", "gzip", "hashlib",
        "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr",
        "imp", "importlib", "inspect", "io", "ipaddress", "itertools",
        "json", "keyword", "lib2to3", "linecache", "locale", "logging",
        "lzma", "mailbox", "mailcap", "marshal", "math", "mimetypes",
        "mmap", "modulefinder", "multiprocessing", "netrc", "nis",
        "nntplib", "numbers", "operator", "optparse", "os", "ossaudiodev",
        "pathlib", "pdb", "pickle", "pickletools", "pipes", "pkgutil",
        "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
        "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr",
        "pydoc", "queue", "quopri", "random", "re", "readline", "reprlib",
        "resource", "rlcompleter", "runpy", "sched", "secrets", "select",
        "selectors", "shelve", "shlex", "shutil", "signal", "site",
        "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "spwd",
        "sqlite3", "ssl", "stat", "statistics", "string", "stringprep",
        "struct", "subprocess", "sunau", "symtable", "sys", "sysconfig",
        "syslog", "tabnanny", "tarfile", "telnetlib", "tempfile", "termios",
        "test", "textwrap", "threading", "time", "timeit", "tkinter",
        "token", "tokenize", "trace", "traceback", "tracemalloc", "tty",
        "turtle", "turtledemo", "types", "typing", "unicodedata", "unittest",
        "urllib", "uu", "uuid", "venv", "warnings", "wave", "weakref",
        "webbrowser", "winreg", "winsound", "wsgiref", "xdrlib", "xml",
        "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib",
    })

    # Import name -> PyPI package name
    IMPORT_TO_PACKAGE = {
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "sklearn": "scikit-learn",
        "yaml": "PyYAML",
        "bs4": "beautifulsoup4",
        "dotenv": "python-dotenv",
        "jwt": "PyJWT",
        "dateutil": "python-dateutil",
        "magic": "python-magic",
        "gi": "PyGObject",
        "OpenSSL": "pyOpenSSL",
        "Crypto": "pycryptodome",
        "serial": "pyserial",
        "usb": "pyusb",
        "dns": "dnspython",
        "wx": "wxPython",
        "lxml": "lxml",
        "numpy": "numpy",
        "pandas": "pandas",
        "scipy": "scipy",
        "matplotlib": "matplotlib",
        "torch": "torch",
        "tensorflow": "tensorflow",
        "keras": "keras",
        "flask": "Flask",
        "django": "Django",
        "fastapi": "fastapi",
        "requests": "requests",
        "aiohttp": "aiohttp",
        "httpx": "httpx",
        "sqlalchemy": "SQLAlchemy",
        "redis": "redis",
        "celery": "celery",
        "pydantic": "pydantic",
        "pytest": "pytest",
        "black": "black",
        "isort": "isort",
        "mypy": "mypy",
        "flake8": "flake8",
        "pylint": "pylint",
        "rich": "rich",
        "typer": "typer",
        "click": "click",
        "toml": "toml",
        "langchain": "langchain",
        "openai": "openai",
        "anthropic": "anthropic",
        "chromadb": "chromadb",
        "langfuse": "langfuse",
        "helicone": "helicone",
        "playwright": "playwright",
        "pyautogui": "pyautogui",
        "speechrecognition": "SpeechRecognition",
        "sounddevice": "sounddevice",
        "soundfile": "soundfile",
        "webrtcvad": "webrtcvad",
    }

    def __init__(self, project_root: Optional[Path] = None):
        """
        Initialize dependency tracker.

        Args:
            project_root: Root directory of the project
        """
        self._project_root = project_root or Path.cwd()
        self._requirements_file = self._project_root / "requirements.txt"
        self._lock = asyncio.Lock()
        self._known_deps: Dict[str, DependencyInfo] = {}

    async def scan_project_imports(self) -> Dict[str, DependencyInfo]:
        """
        Scan entire project for imports and categorize them.

        Returns:
            Dict mapping import names to DependencyInfo
        """
        import ast

        imports: Dict[str, DependencyInfo] = {}

        for py_file in self._project_root.rglob("*.py"):
            if "__pycache__" in str(py_file) or ".venv" in str(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            module_name = alias.name.split(".")[0]
                            if module_name not in imports:
                                imports[module_name] = self._create_dep_info(
                                    module_name, str(py_file)
                                )

                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            module_name = node.module.split(".")[0]
                            if module_name not in imports:
                                imports[module_name] = self._create_dep_info(
                                    module_name, str(py_file)
                                )

            except Exception as e:
                logger.debug(f"Failed to parse {py_file}: {e}")

        self._known_deps = imports
        return imports

    def _create_dep_info(self, import_name: str, first_file: str) -> DependencyInfo:
        """Create DependencyInfo for an import."""
        is_stdlib = import_name in self.STDLIB_MODULES

        # Check if it's a local module
        is_local = False
        local_path = self._project_root / import_name
        local_file = self._project_root / f"{import_name}.py"
        if local_path.exists() or local_file.exists():
            is_local = True

        # Get package name
        package_name = self.IMPORT_TO_PACKAGE.get(import_name, import_name)

        # Try to get installed version
        version_spec = None
        if not is_stdlib and not is_local:
            version_spec = self._get_installed_version(package_name)

        return DependencyInfo(
            name=package_name,
            import_name=import_name,
            version_spec=version_spec,
            is_stdlib=is_stdlib,
            is_local=is_local,
            first_seen_file=first_file,
        )

    def _get_installed_version(self, package_name: str) -> Optional[str]:
        """Get the installed version of a package."""
        try:
            import importlib.metadata
            version = importlib.metadata.version(package_name)
            return f">={version}"
        except Exception:
            return None

    async def update_requirements_txt(
        self,
        add_new: bool = True,
        pin_versions: bool = True,
        remove_unused: bool = False,
    ) -> Dict[str, str]:
        """
        Update requirements.txt based on project imports.

        Args:
            add_new: Add newly detected dependencies
            pin_versions: Pin to currently installed versions
            remove_unused: Remove packages not imported (DANGEROUS)

        Returns:
            Dict of changes made {package: "added"/"updated"/"removed"}
        """
        async with self._lock:
            changes = {}

            # Scan current imports
            if not self._known_deps:
                await self.scan_project_imports()

            # Read existing requirements
            existing_reqs = self._parse_requirements_txt()

            # Determine third-party deps
            third_party = {
                name: info for name, info in self._known_deps.items()
                if not info.is_stdlib and not info.is_local
            }

            # Build new requirements
            new_reqs: Dict[str, str] = {}

            for import_name, info in third_party.items():
                package_name = info.name

                if package_name in existing_reqs:
                    # Keep existing version spec
                    new_reqs[package_name] = existing_reqs[package_name]
                elif add_new:
                    # Add new dependency
                    if pin_versions and info.version_spec:
                        new_reqs[package_name] = info.version_spec
                    else:
                        new_reqs[package_name] = ""
                    changes[package_name] = "added"

            # Keep existing reqs that we didn't detect (could be indirect deps)
            for pkg, ver in existing_reqs.items():
                if pkg not in new_reqs:
                    if remove_unused:
                        changes[pkg] = "removed"
                    else:
                        new_reqs[pkg] = ver

            # Write requirements.txt
            self._write_requirements_txt(new_reqs)

            return changes

    def _parse_requirements_txt(self) -> Dict[str, str]:
        """Parse requirements.txt into dict of package -> version spec."""
        reqs = {}

        if not self._requirements_file.exists():
            return reqs

        try:
            content = self._requirements_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue

                # Parse package==version, package>=version, etc.
                for op in ["==", ">=", "<=", "~=", "!=", ">", "<"]:
                    if op in line:
                        pkg, ver = line.split(op, 1)
                        reqs[pkg.strip()] = op + ver.strip()
                        break
                else:
                    # No version specifier
                    reqs[line] = ""

        except Exception as e:
            logger.error(f"Failed to parse requirements.txt: {e}")

        return reqs

    def _write_requirements_txt(self, reqs: Dict[str, str]) -> None:
        """Write requirements.txt from dict."""
        lines = []
        lines.append("# Auto-generated by JARVIS Ouroboros DependencyTracker")
        lines.append(f"# Last updated: {datetime.now().isoformat()}")
        lines.append("")

        for pkg in sorted(reqs.keys()):
            ver = reqs[pkg]
            if ver:
                lines.append(f"{pkg}{ver}")
            else:
                lines.append(pkg)

        self._requirements_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    async def check_for_new_imports(
        self,
        file_path: str,
        content: str,
    ) -> List[DependencyInfo]:
        """
        Check if a file has new imports not in requirements.txt.

        Args:
            file_path: Path to the file
            content: File content

        Returns:
            List of new dependencies found
        """
        import ast

        new_deps = []
        existing = self._parse_requirements_txt()

        try:
            tree = ast.parse(content)

            for node in ast.walk(tree):
                import_name = None

                if isinstance(node, ast.Import):
                    for alias in node.names:
                        import_name = alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    import_name = node.module.split(".")[0]

                if import_name:
                    dep_info = self._create_dep_info(import_name, file_path)

                    if not dep_info.is_stdlib and not dep_info.is_local:
                        if dep_info.name not in existing:
                            new_deps.append(dep_info)

        except Exception as e:
            logger.debug(f"Failed to check imports: {e}")

        return new_deps


class ProactiveSuggestionEngine:
    """
    v3.1: Background analyzer that proactively suggests code improvements.

    Features:
    - Async background scanning of modified files
    - Pattern-based issue detection
    - Performance bottleneck identification
    - Security vulnerability hints
    - Code smell detection
    - Prioritized suggestion queue
    """

    class SuggestionPriority(Enum):
        CRITICAL = 1  # Security issues, crash bugs
        HIGH = 2      # Performance issues, bad patterns
        MEDIUM = 3    # Code smells, maintainability
        LOW = 4       # Style issues, minor improvements

    @dataclass
    class Suggestion:
        id: str
        file_path: str
        line: Optional[int]
        priority: 'ProactiveSuggestionEngine.SuggestionPriority'
        category: str  # security, performance, style, pattern
        title: str
        description: str
        suggested_fix: Optional[str] = None
        created_at: float = field(default_factory=time.time)

    # Patterns to detect (category, regex, priority, title, description)
    PATTERNS = [
        # Security
        ("security", r"eval\s*\(", SuggestionPriority.CRITICAL,
         "Dangerous eval() usage",
         "eval() can execute arbitrary code. Consider using ast.literal_eval() or a safer alternative."),

        ("security", r"exec\s*\(", SuggestionPriority.CRITICAL,
         "Dangerous exec() usage",
         "exec() can execute arbitrary code. This is a security risk."),

        ("security", r"subprocess\.(call|run|Popen)\s*\([^)]*shell\s*=\s*True",
         SuggestionPriority.CRITICAL,
         "Shell injection risk",
         "shell=True in subprocess can lead to command injection. Use shell=False with a list of arguments."),

        ("security", r"pickle\.(load|loads)\s*\(", SuggestionPriority.HIGH,
         "Insecure deserialization",
         "pickle can execute arbitrary code during deserialization. Consider using json or a safer format."),

        ("security", r"password\s*=\s*['\"][^'\"]+['\"]", SuggestionPriority.CRITICAL,
         "Hardcoded password detected",
         "Passwords should not be hardcoded. Use environment variables or a secrets manager."),

        # Performance
        ("performance", r"for\s+\w+\s+in\s+range\(len\(", SuggestionPriority.MEDIUM,
         "Inefficient iteration pattern",
         "Use 'for item in iterable' or 'for i, item in enumerate(iterable)' instead."),

        ("performance", r"\+\s*=.*\+.*in.*for", SuggestionPriority.MEDIUM,
         "String concatenation in loop",
         "String concatenation in loops is O(n²). Use ''.join() or a list."),

        ("performance", r"time\.sleep\s*\(\s*\d+\s*\)", SuggestionPriority.LOW,
         "Blocking sleep in async context",
         "time.sleep() blocks the event loop. Use await asyncio.sleep() in async code."),

        # Code patterns
        ("pattern", r"except\s*:", SuggestionPriority.MEDIUM,
         "Bare except clause",
         "Catching all exceptions can hide bugs. Specify the exception types."),

        ("pattern", r"except\s+Exception\s*:", SuggestionPriority.LOW,
         "Overly broad exception handling",
         "Consider catching more specific exceptions."),

        ("pattern", r"TODO|FIXME|XXX|HACK", SuggestionPriority.LOW,
         "TODO/FIXME comment found",
         "There's a TODO or FIXME that should be addressed."),

        ("pattern", r"print\s*\(", SuggestionPriority.LOW,
         "Print statement in production code",
         "Consider using logging instead of print for production code."),

        # Style
        ("style", r"^class\s+\w+\s*:", SuggestionPriority.LOW,
         "Class without docstring",
         "Classes should have docstrings explaining their purpose."),
    ]

    def __init__(self, project_root: Optional[Path] = None):
        """Initialize proactive suggestion engine."""
        self._project_root = project_root or Path.cwd()
        self._suggestions: Dict[str, 'ProactiveSuggestionEngine.Suggestion'] = {}
        self._lock = asyncio.Lock()
        self._scan_task: Optional[asyncio.Task] = None
        self._running = False
        self._file_hashes: Dict[str, str] = {}

    async def start_background_scan(
        self,
        interval_seconds: float = 60.0,
    ) -> None:
        """Start background scanning for issues."""
        self._running = True
        self._scan_task = asyncio.create_task(
            self._background_scanner(interval_seconds)
        )
        logger.info("ProactiveSuggestionEngine background scan started")

    async def stop_background_scan(self) -> None:
        """Stop background scanning."""
        self._running = False
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        logger.info("ProactiveSuggestionEngine background scan stopped")

    async def _background_scanner(self, interval: float) -> None:
        """Background task that scans for issues."""
        while self._running:
            try:
                await self._scan_modified_files()
            except Exception as e:
                logger.error(f"Background scan error: {e}")

            await asyncio.sleep(interval)

    async def _scan_modified_files(self) -> None:
        """Scan recently modified files for issues."""
        for py_file in self._project_root.rglob("*.py"):
            if "__pycache__" in str(py_file) or ".venv" in str(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
                content_hash = hashlib.md5(content.encode()).hexdigest()

                # Skip if file hasn't changed
                if self._file_hashes.get(str(py_file)) == content_hash:
                    continue

                self._file_hashes[str(py_file)] = content_hash

                # Scan for issues
                await self.analyze_file(str(py_file), content)

            except Exception as e:
                logger.debug(f"Failed to scan {py_file}: {e}")

    async def analyze_file(
        self,
        file_path: str,
        content: Optional[str] = None,
    ) -> List['ProactiveSuggestionEngine.Suggestion']:
        """
        Analyze a file for issues and return suggestions.

        Args:
            file_path: Path to the file
            content: Optional file content (reads from disk if not provided)

        Returns:
            List of suggestions for the file
        """
        import re

        if content is None:
            content = Path(file_path).read_text(encoding="utf-8")

        suggestions = []
        lines = content.splitlines()

        for line_num, line in enumerate(lines, 1):
            for category, pattern, priority, title, description in self.PATTERNS:
                if re.search(pattern, line):
                    suggestion = self.Suggestion(
                        id=f"suggest_{uuid.uuid4().hex[:8]}",
                        file_path=file_path,
                        line=line_num,
                        priority=priority,
                        category=category,
                        title=title,
                        description=description,
                    )
                    suggestions.append(suggestion)

                    async with self._lock:
                        self._suggestions[suggestion.id] = suggestion

        return suggestions

    async def get_suggestions(
        self,
        file_path: Optional[str] = None,
        min_priority: Optional['ProactiveSuggestionEngine.SuggestionPriority'] = None,
        category: Optional[str] = None,
    ) -> List['ProactiveSuggestionEngine.Suggestion']:
        """Get current suggestions, optionally filtered."""
        async with self._lock:
            suggestions = list(self._suggestions.values())

        if file_path:
            suggestions = [s for s in suggestions if s.file_path == file_path]

        if min_priority:
            suggestions = [s for s in suggestions if s.priority.value <= min_priority.value]

        if category:
            suggestions = [s for s in suggestions if s.category == category]

        # Sort by priority
        suggestions.sort(key=lambda s: (s.priority.value, s.created_at))

        return suggestions

    async def dismiss_suggestion(self, suggestion_id: str) -> bool:
        """Dismiss a suggestion."""
        async with self._lock:
            return self._suggestions.pop(suggestion_id, None) is not None

    async def get_summary(self) -> Dict[str, Any]:
        """Get summary of all suggestions."""
        async with self._lock:
            suggestions = list(self._suggestions.values())

        return {
            "total": len(suggestions),
            "by_priority": {
                "critical": len([s for s in suggestions if s.priority == self.SuggestionPriority.CRITICAL]),
                "high": len([s for s in suggestions if s.priority == self.SuggestionPriority.HIGH]),
                "medium": len([s for s in suggestions if s.priority == self.SuggestionPriority.MEDIUM]),
                "low": len([s for s in suggestions if s.priority == self.SuggestionPriority.LOW]),
            },
            "by_category": {
                "security": len([s for s in suggestions if s.category == "security"]),
                "performance": len([s for s in suggestions if s.category == "performance"]),
                "pattern": len([s for s in suggestions if s.category == "pattern"]),
                "style": len([s for s in suggestions if s.category == "style"]),
            },
            "files_affected": len(set(s.file_path for s in suggestions)),
        }


class DocumentationGenerator:
    """
    v3.1: Automatic documentation generation for undocumented code.

    Features:
    - Detect undocumented functions, classes, modules
    - Generate docstrings using LLM or templates
    - Support multiple docstring formats (Google, NumPy, Sphinx)
    - Analyze function signatures for parameter documentation
    - Generate README sections
    """

    class DocstringFormat(Enum):
        GOOGLE = "google"
        NUMPY = "numpy"
        SPHINX = "sphinx"
        SIMPLE = "simple"

    def __init__(
        self,
        format_style: 'DocumentationGenerator.DocstringFormat' = None,
        llm_client: Optional[Any] = None,
    ):
        """
        Initialize documentation generator.

        Args:
            format_style: Docstring format to use
            llm_client: Optional LLM client for AI-generated docs
        """
        self._format = format_style or self.DocstringFormat.GOOGLE
        self._llm_client = llm_client

    async def find_undocumented(
        self,
        file_path: str,
        content: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find all undocumented functions, classes, and methods.

        Returns:
            List of dicts with 'type', 'name', 'line', 'signature'
        """
        import ast

        if content is None:
            content = Path(file_path).read_text(encoding="utf-8")

        undocumented = []

        try:
            tree = ast.parse(content)

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not ast.get_docstring(node):
                        undocumented.append({
                            "type": "function",
                            "name": node.name,
                            "line": node.lineno,
                            "signature": self._get_signature(node),
                            "is_method": self._is_method(node, tree),
                        })

                elif isinstance(node, ast.ClassDef):
                    if not ast.get_docstring(node):
                        undocumented.append({
                            "type": "class",
                            "name": node.name,
                            "line": node.lineno,
                            "bases": [self._get_name(b) for b in node.bases],
                        })

        except Exception as e:
            logger.error(f"Failed to parse {file_path}: {e}")

        return undocumented

    def _get_signature(self, node: Any) -> str:
        """Get function signature string."""
        import ast

        params = []
        for arg in node.args.args:
            param = arg.arg
            if arg.annotation:
                param += f": {ast.unparse(arg.annotation)}"
            params.append(param)

        for i, default in enumerate(reversed(node.args.defaults)):
            idx = len(params) - i - 1
            params[idx] += f" = {ast.unparse(default)}"

        if node.args.vararg:
            params.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg:
            params.append(f"**{node.args.kwarg.arg}")

        return_type = ""
        if node.returns:
            return_type = f" -> {ast.unparse(node.returns)}"

        return f"({', '.join(params)}){return_type}"

    def _is_method(self, node: Any, tree: Any) -> bool:
        """Check if a function is a method (inside a class)."""
        import ast

        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef):
                for child in parent.body:
                    if child is node:
                        return True
        return False

    def _get_name(self, node: Any) -> str:
        """Get name from a node."""
        import ast

        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_name(node.value)}.{node.attr}"
        return str(node)

    async def generate_docstring(
        self,
        item: Dict[str, Any],
        context: Optional[str] = None,
    ) -> str:
        """
        Generate a docstring for an undocumented item.

        Args:
            item: Item info from find_undocumented()
            context: Optional surrounding code context

        Returns:
            Generated docstring
        """
        if self._llm_client:
            return await self._generate_with_llm(item, context)

        return self._generate_template_docstring(item)

    def _generate_template_docstring(self, item: Dict[str, Any]) -> str:
        """Generate a template docstring without LLM."""
        if item["type"] == "class":
            if self._format == self.DocstringFormat.GOOGLE:
                return f'"""{item["name"]} class.\n\n    TODO: Add class description.\n    """'
            return f'"""{item["name"]} class."""'

        # Function/method
        signature = item.get("signature", "()")

        # Parse parameters from signature
        params = self._parse_params(signature)

        if self._format == self.DocstringFormat.GOOGLE:
            return self._google_docstring(item["name"], params)
        elif self._format == self.DocstringFormat.NUMPY:
            return self._numpy_docstring(item["name"], params)
        elif self._format == self.DocstringFormat.SPHINX:
            return self._sphinx_docstring(item["name"], params)

        return f'"""TODO: Document {item["name"]}."""'

    def _parse_params(self, signature: str) -> List[Dict[str, str]]:
        """Parse parameters from signature string."""
        params = []

        # Remove parentheses and return type
        sig = signature.strip("()")
        if " -> " in sig:
            sig = sig.split(" -> ")[0]

        if not sig or sig == "self":
            return params

        for part in sig.split(","):
            part = part.strip()
            if part in ("self", "cls"):
                continue

            name = part.split(":")[0].split("=")[0].strip()
            type_hint = ""
            default = ""

            if ":" in part:
                type_hint = part.split(":")[1].split("=")[0].strip()
            if "=" in part:
                default = part.split("=")[1].strip()

            params.append({
                "name": name,
                "type": type_hint,
                "default": default,
            })

        return params

    def _google_docstring(self, name: str, params: List[Dict]) -> str:
        """Generate Google-style docstring."""
        lines = [f'"""TODO: Describe {name}.', ""]

        if params:
            lines.append("    Args:")
            for p in params:
                type_str = f" ({p['type']})" if p['type'] else ""
                default_str = f" Defaults to {p['default']}." if p['default'] else ""
                lines.append(f"        {p['name']}{type_str}: TODO: Describe.{default_str}")
            lines.append("")

        lines.append("    Returns:")
        lines.append("        TODO: Describe return value.")
        lines.append('    """')

        return "\n".join(lines)

    def _numpy_docstring(self, name: str, params: List[Dict]) -> str:
        """Generate NumPy-style docstring."""
        lines = [f'"""TODO: Describe {name}.', ""]

        if params:
            lines.append("    Parameters")
            lines.append("    ----------")
            for p in params:
                type_str = f" : {p['type']}" if p['type'] else ""
                lines.append(f"    {p['name']}{type_str}")
                lines.append("        TODO: Describe.")
            lines.append("")

        lines.append("    Returns")
        lines.append("    -------")
        lines.append("    TODO")
        lines.append("        TODO: Describe return value.")
        lines.append('    """')

        return "\n".join(lines)

    def _sphinx_docstring(self, name: str, params: List[Dict]) -> str:
        """Generate Sphinx-style docstring."""
        lines = [f'"""TODO: Describe {name}.', ""]

        for p in params:
            type_str = f" {p['type']}" if p['type'] else ""
            lines.append(f"    :param{type_str} {p['name']}: TODO: Describe.")

        lines.append("    :returns: TODO: Describe return value.")
        lines.append('    """')

        return "\n".join(lines)

    async def _generate_with_llm(
        self,
        item: Dict[str, Any],
        context: Optional[str],
    ) -> str:
        """Generate docstring using LLM."""
        # This would integrate with the Trinity LLM system
        # For now, fall back to template
        return self._generate_template_docstring(item)

    async def document_file(
        self,
        file_path: str,
        dry_run: bool = True,
    ) -> Dict[str, Any]:
        """
        Add docstrings to all undocumented items in a file.

        Args:
            file_path: Path to the file
            dry_run: If True, return changes without applying

        Returns:
            Dict with 'changes' list and 'new_content'
        """
        content = Path(file_path).read_text(encoding="utf-8")
        undocumented = await self.find_undocumented(file_path, content)

        if not undocumented:
            return {"changes": [], "new_content": content}

        changes = []
        lines = content.splitlines(keepends=True)

        # Process in reverse order to maintain line numbers
        for item in sorted(undocumented, key=lambda x: x["line"], reverse=True):
            docstring = await self.generate_docstring(item, content)

            # Find the line to insert after (the def/class line)
            line_idx = item["line"] - 1

            # Find the indentation
            def_line = lines[line_idx]
            indent = len(def_line) - len(def_line.lstrip())
            doc_indent = " " * (indent + 4)

            # Format docstring with proper indentation
            doc_lines = docstring.splitlines()
            formatted_doc = doc_lines[0] + "\n"
            for dl in doc_lines[1:]:
                formatted_doc += doc_indent + dl + "\n"

            # Insert after the def line (account for multi-line defs)
            insert_idx = line_idx + 1
            while insert_idx < len(lines) and not lines[insert_idx - 1].rstrip().endswith(":"):
                insert_idx += 1

            lines.insert(insert_idx, doc_indent + formatted_doc)

            changes.append({
                "type": item["type"],
                "name": item["name"],
                "line": item["line"],
                "docstring": docstring,
            })

        new_content = "".join(lines)

        if not dry_run:
            Path(file_path).write_text(new_content, encoding="utf-8")

        return {"changes": changes, "new_content": new_content}


class VoiceCommandRouter:
    """
    v3.1: Routes voice commands to appropriate self-programming handlers.

    Provides natural language understanding for:
    - File creation ("create a hello world program")
    - Code improvement ("optimize this function")
    - Refactoring ("rename this function to X")
    - Documentation ("add docstrings to this file")
    - Dependency management ("update requirements")
    """

    # Command patterns for routing
    PATTERNS = {
        "create_file": [
            r"create\s+(a\s+)?(?:new\s+)?(?:file|program|script|module|class)\s+(?:called\s+)?(.+)",
            r"write\s+(?:me\s+)?(?:a\s+)?(.+)\s+(?:file|program|script)",
            r"make\s+(?:a\s+)?(?:new\s+)?(.+)",
            r"generate\s+(?:a\s+)?(.+)",
        ],
        "rename": [
            r"rename\s+(?:this\s+)?(?:function|class|variable|method)\s+(?:to\s+)?(\w+)",
            r"change\s+(?:the\s+)?name\s+(?:of\s+)?(.+)\s+to\s+(\w+)",
        ],
        "improve": [
            r"improve\s+(?:this\s+)?(?:code|function|file)",
            r"optimize\s+(?:this\s+)?(?:code|function|file)",
            r"refactor\s+(?:this\s+)?(?:code|function|file)",
            r"make\s+(?:this\s+)?(?:code|function)\s+(?:better|faster|cleaner)",
        ],
        "document": [
            r"add\s+(?:docstrings?|documentation)\s+(?:to\s+)?(?:this\s+)?(?:file|code)?",
            r"document\s+(?:this\s+)?(?:file|code|function|class)",
            r"generate\s+(?:docstrings?|documentation)",
        ],
        "update_deps": [
            r"update\s+(?:the\s+)?requirements",
            r"add\s+(?:missing\s+)?dependencies",
            r"sync\s+(?:the\s+)?requirements",
        ],
        "analyze": [
            r"analyze\s+(?:this\s+)?(?:code|file)",
            r"check\s+(?:for\s+)?(?:issues|problems|bugs)",
            r"find\s+(?:issues|problems|bugs)\s+(?:in\s+)?(?:this\s+)?(?:code|file)?",
        ],
    }

    @dataclass
    class ParsedCommand:
        """Parsed voice command."""
        action: str
        parameters: Dict[str, Any]
        confidence: float
        raw_text: str

    def __init__(
        self,
        engine: Optional[Any] = None,
        refactoring_engine: Optional[AdvancedRefactoringEngine] = None,
        dependency_tracker: Optional[DependencyTracker] = None,
        suggestion_engine: Optional[ProactiveSuggestionEngine] = None,
        documentation_generator: Optional[DocumentationGenerator] = None,
    ):
        """
        Initialize voice command router.

        Args:
            engine: OuroborosEngine for file creation/improvement
            refactoring_engine: For rename/refactor operations
            dependency_tracker: For requirements management
            suggestion_engine: For code analysis
            documentation_generator: For docstring generation
        """
        self._engine = engine
        self._refactoring = refactoring_engine
        self._deps = dependency_tracker
        self._suggestions = suggestion_engine
        self._docs = documentation_generator

    def parse_command(self, text: str) -> Optional['VoiceCommandRouter.ParsedCommand']:
        """
        Parse a voice command into a structured format.

        Args:
            text: Raw voice command text

        Returns:
            ParsedCommand or None if not recognized
        """
        import re

        text_lower = text.lower().strip()

        for action, patterns in self.PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, text_lower)
                if match:
                    params = {}
                    groups = match.groups()

                    if action == "create_file":
                        # Extract file description
                        desc = groups[-1] if groups else text
                        params["description"] = desc.strip()

                    elif action == "rename":
                        if len(groups) >= 2:
                            params["old_name"] = groups[0]
                            params["new_name"] = groups[1]
                        else:
                            params["new_name"] = groups[0] if groups else ""

                    return self.ParsedCommand(
                        action=action,
                        parameters=params,
                        confidence=0.8,  # Could use fuzzy matching score
                        raw_text=text,
                    )

        return None

    async def execute_command(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a parsed voice command.

        Args:
            command: Parsed command to execute
            context: Optional context (current file, selection, etc.)

        Returns:
            Result dict with 'success', 'message', and action-specific data
        """
        context = context or {}

        if command.action == "create_file":
            return await self._handle_create_file(command, context)
        elif command.action == "rename":
            return await self._handle_rename(command, context)
        elif command.action == "improve":
            return await self._handle_improve(command, context)
        elif command.action == "document":
            return await self._handle_document(command, context)
        elif command.action == "update_deps":
            return await self._handle_update_deps(command, context)
        elif command.action == "analyze":
            return await self._handle_analyze(command, context)

        return {
            "success": False,
            "message": f"Unknown command action: {command.action}",
        }

    async def _handle_create_file(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle file creation command."""
        if not self._engine:
            return {"success": False, "message": "Engine not available"}

        description = command.parameters.get("description", "")

        # Try to infer file path from description
        file_path = self._infer_file_path(description, context)

        # Import FileCreationRequest from engine
        try:
            from backend.core.ouroboros.engine import FileCreationRequest
            request = FileCreationRequest(
                target_path=Path(file_path),
                description=description,
                generate_tests=True,
            )
            result = await self._engine.create_file(request)

            return {
                "success": result.success,
                "message": f"Created {file_path}" if result.success else "Failed to create file",
                "file_path": file_path,
                "result": result,
            }
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}

    def _infer_file_path(
        self,
        description: str,
        context: Dict[str, Any],
    ) -> str:
        """Infer a file path from a description."""
        import re

        # Check if description contains a path
        path_match = re.search(r'(?:at|in|to)\s+([^\s]+\.py)', description.lower())
        if path_match:
            return path_match.group(1)

        # Generate from description
        name = description.lower()
        name = re.sub(r'[^\w\s]', '', name)
        name = re.sub(r'\s+', '_', name)
        name = name[:50]  # Limit length

        if not name.endswith('.py'):
            name += '.py'

        # Use context directory if available
        base_dir = context.get("current_dir", ".")
        return str(Path(base_dir) / name)

    async def _handle_rename(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle rename command."""
        if not self._refactoring:
            return {"success": False, "message": "Refactoring engine not available"}

        new_name = command.parameters.get("new_name")
        if not new_name:
            return {"success": False, "message": "New name not specified"}

        # Need context for current position
        file_path = context.get("current_file")
        line = context.get("cursor_line", 1)
        column = context.get("cursor_column", 1)

        if not file_path:
            return {"success": False, "message": "No file context available"}

        try:
            plan = await self._refactoring.rename_symbol(
                file_path, line, column, new_name, dry_run=False
            )
            return {
                "success": True,
                "message": f"Renamed to {new_name} in {len(plan.affected_files)} files",
                "plan": plan,
            }
        except Exception as e:
            return {"success": False, "message": f"Rename failed: {e}"}

    async def _handle_improve(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle code improvement command."""
        if not self._engine:
            return {"success": False, "message": "Engine not available"}

        file_path = context.get("current_file")
        if not file_path:
            return {"success": False, "message": "No file context available"}

        try:
            from backend.core.ouroboros.engine import ImprovementRequest
            request = ImprovementRequest(
                target_file=Path(file_path),
                goal="Improve code quality, performance, and readability",
            )
            result = await self._engine.improve(request)

            return {
                "success": result.success,
                "message": "Code improved" if result.success else "Improvement failed",
                "result": result,
            }
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}

    async def _handle_document(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle documentation command."""
        if not self._docs:
            self._docs = DocumentationGenerator()

        file_path = context.get("current_file")
        if not file_path:
            return {"success": False, "message": "No file context available"}

        try:
            result = await self._docs.document_file(file_path, dry_run=False)
            return {
                "success": True,
                "message": f"Added {len(result['changes'])} docstrings",
                "changes": result["changes"],
            }
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}

    async def _handle_update_deps(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle dependency update command."""
        if not self._deps:
            self._deps = DependencyTracker()

        try:
            changes = await self._deps.update_requirements_txt()
            return {
                "success": True,
                "message": f"Updated requirements.txt: {len(changes)} changes",
                "changes": changes,
            }
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}

    async def _handle_analyze(
        self,
        command: 'VoiceCommandRouter.ParsedCommand',
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Handle code analysis command."""
        if not self._suggestions:
            self._suggestions = ProactiveSuggestionEngine()

        file_path = context.get("current_file")

        try:
            if file_path:
                suggestions = await self._suggestions.analyze_file(file_path)
            else:
                suggestions = await self._suggestions.get_suggestions()

            return {
                "success": True,
                "message": f"Found {len(suggestions)} issues",
                "suggestions": suggestions,
            }
        except Exception as e:
            return {"success": False, "message": f"Error: {e}"}


# Global instances
_refactoring_engine: Optional[AdvancedRefactoringEngine] = None
_dependency_tracker: Optional[DependencyTracker] = None
_suggestion_engine: Optional[ProactiveSuggestionEngine] = None
_documentation_generator: Optional[DocumentationGenerator] = None
_voice_command_router: Optional[VoiceCommandRouter] = None


def get_refactoring_engine() -> AdvancedRefactoringEngine:
    """Get or create global refactoring engine."""
    global _refactoring_engine
    if _refactoring_engine is None:
        _refactoring_engine = AdvancedRefactoringEngine()
    return _refactoring_engine


def get_dependency_tracker(project_root: Optional[Path] = None) -> DependencyTracker:
    """Get or create global dependency tracker."""
    global _dependency_tracker
    if _dependency_tracker is None:
        _dependency_tracker = DependencyTracker(project_root)
    return _dependency_tracker


def get_suggestion_engine(project_root: Optional[Path] = None) -> ProactiveSuggestionEngine:
    """Get or create global suggestion engine."""
    global _suggestion_engine
    if _suggestion_engine is None:
        _suggestion_engine = ProactiveSuggestionEngine(project_root)
    return _suggestion_engine


def get_documentation_generator() -> DocumentationGenerator:
    """Get or create global documentation generator."""
    global _documentation_generator
    if _documentation_generator is None:
        _documentation_generator = DocumentationGenerator()
    return _documentation_generator


def get_voice_command_router(
    engine: Optional[Any] = None,
) -> VoiceCommandRouter:
    """Get or create global voice command router."""
    global _voice_command_router
    if _voice_command_router is None:
        _voice_command_router = VoiceCommandRouter(
            engine=engine,
            refactoring_engine=get_refactoring_engine(),
            dependency_tracker=get_dependency_tracker(),
            suggestion_engine=get_suggestion_engine(),
            documentation_generator=get_documentation_generator(),
        )
    return _voice_command_router


class MultiFileOrchestrator:
    """
    Orchestrates atomic multi-file editing sessions.

    Features:
    - Plan multi-file changes based on blast radius
    - Apply all changes atomically (all or nothing)
    - Track dependencies between files
    - Rollback on any failure
    """

    def __init__(self, session_memory: SessionMemoryManager):
        self._session_memory = session_memory
        self._pending_changes: Dict[str, Tuple[str, str]] = {}  # path -> (original, new)
        self._lock = asyncio.Lock()
        self._transaction_id: Optional[str] = None

    async def begin_transaction(self) -> str:
        """Begin a multi-file transaction."""
        async with self._lock:
            self._transaction_id = f"txn_{uuid.uuid4().hex[:12]}"
            self._pending_changes.clear()
            return self._transaction_id

    async def stage_change(
        self,
        file_path: str,
        original_content: str,
        new_content: str,
    ) -> None:
        """Stage a file change for the current transaction."""
        async with self._lock:
            if not self._transaction_id:
                raise RuntimeError("No active transaction - call begin_transaction() first")
            self._pending_changes[file_path] = (original_content, new_content)

    async def commit_transaction(self, goal: str) -> Tuple[bool, Optional[str]]:
        """
        Commit all staged changes atomically.

        Returns (success, error_message).
        """
        async with self._lock:
            if not self._transaction_id:
                return False, "No active transaction"

            if not self._pending_changes:
                return True, None  # Nothing to commit

            # Create backups
            backups: Dict[str, str] = {}
            applied: List[str] = []

            try:
                # Apply all changes
                for file_path, (original, new_content) in self._pending_changes.items():
                    path = Path(file_path)
                    if path.exists():
                        backups[file_path] = path.read_text(encoding="utf-8")

                    path.write_text(new_content, encoding="utf-8")
                    applied.append(file_path)

                    # Record in session memory
                    await self._session_memory.record_change(
                        file_path=file_path,
                        original_content=original,
                        new_content=new_content,
                        change_type="multi_file_edit",
                        goal=goal,
                        success=True,
                    )

                # Clear transaction state
                self._pending_changes.clear()
                self._transaction_id = None
                return True, None

            except Exception as e:
                # Rollback all applied changes
                for file_path in applied:
                    try:
                        if file_path in backups:
                            Path(file_path).write_text(backups[file_path], encoding="utf-8")
                    except Exception as rollback_error:
                        logger.error(f"Rollback failed for {file_path}: {rollback_error}")

                self._pending_changes.clear()
                self._transaction_id = None
                return False, str(e)

    async def abort_transaction(self) -> None:
        """Abort the current transaction without applying changes."""
        async with self._lock:
            self._pending_changes.clear()
            self._transaction_id = None


class IterativeRefinementLoop:
    """
    Provides user-directed iterative refinement of changes.

    Flow:
    1. Generate initial change
    2. User reviews and provides feedback ("make it smaller", "add error handling", etc.)
    3. Regenerate based on feedback
    4. Repeat until approved
    """

    def __init__(
        self,
        diff_preview_engine: DiffPreviewEngine,
        max_iterations: int = 5,
    ):
        self._diff_preview = diff_preview_engine
        self._max_iterations = max_iterations
        self._iteration_history: List[Dict[str, Any]] = []

    async def refine_with_feedback(
        self,
        original_content: str,
        current_content: str,
        feedback: str,
        goal: str,
        generate_improvement: Callable[[str, str, str], Awaitable[Optional[str]]],
    ) -> Tuple[Optional[str], int]:
        """
        Refine the change based on user feedback.

        Args:
            original_content: Original file content
            current_content: Current proposed change
            feedback: User feedback for refinement
            goal: Original improvement goal
            generate_improvement: Async function to generate improvements

        Returns:
            (refined_content, iteration_count) or (None, iteration_count) on failure
        """
        iteration = 0
        current = current_content

        # Incorporate feedback into the goal
        refined_goal = f"{goal}\n\nUser feedback: {feedback}"

        while iteration < self._max_iterations:
            iteration += 1

            # Generate refined version
            refined = await generate_improvement(original_content, refined_goal, current)

            if not refined:
                logger.warning(f"Refinement iteration {iteration} failed")
                continue

            self._iteration_history.append({
                "iteration": iteration,
                "feedback": feedback,
                "timestamp": time.time(),
                "content_hash": hashlib.md5(refined.encode()).hexdigest()[:12],
            })

            return refined, iteration

        return None, iteration

    def get_iteration_history(self) -> List[Dict[str, Any]]:
        """Get history of refinement iterations."""
        return list(self._iteration_history)


class ConnectionPoolManager:
    """
    Manages connection pools for API calls.

    Features:
    - Reusable HTTP sessions per provider
    - Automatic cleanup of idle connections
    - Health-aware routing
    """

    def __init__(self):
        self._sessions: Dict[str, Any] = {}  # endpoint -> aiohttp.ClientSession
        self._last_used: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._idle_timeout = 300.0  # 5 minutes

    async def get_session(self, endpoint: str) -> Any:
        """Get or create a session for the endpoint."""
        if not aiohttp:
            raise RuntimeError("aiohttp not available")

        async with self._lock:
            if endpoint in self._sessions:
                self._last_used[endpoint] = time.time()
                return self._sessions[endpoint]

            # Create new session with connection pooling
            connector = aiohttp.TCPConnector(
                limit=10,  # Max connections per host
                limit_per_host=5,
                ttl_dns_cache=300,
                keepalive_timeout=30,
            )
            session = aiohttp.ClientSession(connector=connector)
            self._sessions[endpoint] = session
            self._last_used[endpoint] = time.time()

            # Start cleanup task if not running
            if not self._cleanup_task or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            return session

    async def _cleanup_loop(self) -> None:
        """Periodically clean up idle sessions."""
        while True:
            await asyncio.sleep(60)  # Check every minute
            await self._cleanup_idle_sessions()

    async def _cleanup_idle_sessions(self) -> None:
        """Close sessions that have been idle too long."""
        now = time.time()
        to_close = []

        async with self._lock:
            for endpoint, last_used in list(self._last_used.items()):
                if now - last_used > self._idle_timeout:
                    to_close.append(endpoint)

            for endpoint in to_close:
                session = self._sessions.pop(endpoint, None)
                self._last_used.pop(endpoint, None)
                if session:
                    await session.close()
                    logger.debug(f"Closed idle session for {endpoint}")

    async def close_all(self) -> None:
        """Close all sessions."""
        async with self._lock:
            for session in self._sessions.values():
                await session.close()
            self._sessions.clear()
            self._last_used.clear()

            if self._cleanup_task:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass


# =============================================================================
# ENHANCED NATIVE SELF-IMPROVEMENT WITH CLAUDE CODE BEHAVIORS
# =============================================================================

class EnhancedSelfImprovement(NativeSelfImprovement):
    """
    Enhanced self-improvement engine with Claude Code-like behaviors.

    Adds:
    - Diff preview before applying
    - Multi-file orchestration
    - Session memory
    - Streaming changes
    - Iterative refinement
    - Connection pooling
    """

    def __init__(self):
        super().__init__()

        # Claude Code-like components
        self._session_memory = SessionMemoryManager()
        self._diff_preview_engine = DiffPreviewEngine()
        self._connection_pool = ConnectionPoolManager()
        self._multi_file_orchestrator = MultiFileOrchestrator(self._session_memory)
        self._refinement_loop = IterativeRefinementLoop(self._diff_preview_engine)

        # Preview mode settings
        self._require_approval = bool(os.getenv("OUROBOROS_REQUIRE_APPROVAL", "false").lower() in ("true", "1"))
        self._auto_stream_diff = bool(os.getenv("OUROBOROS_STREAM_DIFF", "true").lower() in ("true", "1"))

    @property
    def session_memory(self) -> SessionMemoryManager:
        return self._session_memory

    @property
    def diff_preview_engine(self) -> DiffPreviewEngine:
        return self._diff_preview_engine

    @property
    def multi_file_orchestrator(self) -> MultiFileOrchestrator:
        return self._multi_file_orchestrator

    async def initialize(self) -> None:
        """Initialize with enhanced components."""
        await super().initialize()
        self.logger.info("Enhanced self-improvement components initialized")
        self.logger.info(f"  - Session ID: {self._session_memory.session_id}")
        self.logger.info(f"  - Require approval: {self._require_approval}")
        self.logger.info(f"  - Auto stream diff: {self._auto_stream_diff}")

    async def shutdown(self) -> None:
        """Shutdown with cleanup."""
        await self._connection_pool.close_all()
        await super().shutdown()

    async def execute_with_preview(
        self,
        target: Union[str, Path],
        goal: str,
        test_command: Optional[str] = None,
        context: Optional[str] = None,
        max_iterations: int = 5,
        require_approval: Optional[bool] = None,
    ) -> ImprovementResult:
        """
        Execute improvement with diff preview and approval workflow.

        This is the Claude Code-like interface:
        1. Generate improvement
        2. Create and stream diff preview
        3. Wait for user approval (if require_approval=True)
        4. Apply or iterate based on feedback
        """
        require = require_approval if require_approval is not None else self._require_approval

        # Phase 1: Generate improvement
        target_path = SecurityValidator.validate_path(target)
        original_content = await self._read_file_safe(target_path)

        improved_content, provider = await self._generate_improvement(
            original_content, goal, None, context
        )

        if not improved_content:
            return ImprovementResult(
                success=False,
                task_id=f"imp_{uuid.uuid4().hex[:12]}",
                target_file=str(target_path),
                goal=goal,
                iterations=1,
                total_time=0,
                error="Failed to generate improvement",
            )

        # Phase 2: Create diff preview
        preview = await self._diff_preview_engine.create_preview(
            original_content=original_content,
            modified_content=improved_content,
            file_path=str(target_path),
            goal=goal,
        )

        # Phase 3: Stream diff if enabled
        if self._auto_stream_diff:
            await self._diff_preview_engine.stream_preview(preview)

        # Phase 4: Wait for approval if required
        if require:
            approved, feedback = await self._diff_preview_engine.wait_for_approval(
                preview.id, timeout=300.0
            )

            if not approved:
                if feedback:
                    # User wants refinement
                    refined, iterations = await self._refinement_loop.refine_with_feedback(
                        original_content=original_content,
                        current_content=improved_content,
                        feedback=feedback,
                        goal=goal,
                        generate_improvement=lambda orig, g, _: self._generate_improvement(orig, g, None, context),
                    )
                    if refined:
                        improved_content = refined[0] if isinstance(refined, tuple) else refined
                    else:
                        return ImprovementResult(
                            success=False,
                            task_id=preview.id,
                            target_file=str(target_path),
                            goal=goal,
                            iterations=iterations,
                            total_time=0,
                            error=f"Refinement failed after {iterations} iterations",
                        )
                else:
                    # User rejected
                    return ImprovementResult(
                        success=False,
                        task_id=preview.id,
                        target_file=str(target_path),
                        goal=goal,
                        iterations=0,
                        total_time=0,
                        error="Changes rejected by user",
                    )

        # Phase 5: Apply changes
        await self._write_file_safe(target_path, improved_content)
        await self._session_memory.record_change(
            file_path=str(target_path),
            original_content=original_content,
            new_content=improved_content,
            change_type="improvement",
            goal=goal,
            success=True,
        )

        return ImprovementResult(
            success=True,
            task_id=preview.id,
            target_file=str(target_path),
            goal=goal,
            iterations=1,
            total_time=0,
            provider_used=provider,
            changes_applied=True,
            diff=preview.to_unified_diff(),
        )

    async def execute_multi_file_improvement(
        self,
        files_and_goals: List[Tuple[Union[str, Path], str]],
        shared_context: Optional[str] = None,
        require_approval: bool = True,
    ) -> Dict[str, ImprovementResult]:
        """
        Execute improvements on multiple files atomically.

        All files are modified together or none are modified.
        """
        results: Dict[str, ImprovementResult] = {}

        # Begin transaction
        txn_id = await self._multi_file_orchestrator.begin_transaction()

        try:
            # Generate improvements for each file
            for target, goal in files_and_goals:
                target_path = SecurityValidator.validate_path(target)
                original_content = await self._read_file_safe(target_path)

                improved_content, provider = await self._generate_improvement(
                    original_content, goal, None, shared_context
                )

                if not improved_content:
                    results[str(target_path)] = ImprovementResult(
                        success=False,
                        task_id=txn_id,
                        target_file=str(target_path),
                        goal=goal,
                        iterations=0,
                        total_time=0,
                        error="Failed to generate improvement",
                    )
                    # Abort on any failure
                    await self._multi_file_orchestrator.abort_transaction()
                    return results

                # Stage the change
                await self._multi_file_orchestrator.stage_change(
                    str(target_path), original_content, improved_content
                )

                results[str(target_path)] = ImprovementResult(
                    success=True,
                    task_id=txn_id,
                    target_file=str(target_path),
                    goal=goal,
                    iterations=1,
                    total_time=0,
                    provider_used=provider,
                )

            # Commit all changes atomically
            combined_goal = "; ".join(g for _, g in files_and_goals)
            success, error = await self._multi_file_orchestrator.commit_transaction(combined_goal)

            if not success:
                # Mark all as failed
                for path in results:
                    results[path] = ImprovementResult(
                        success=False,
                        task_id=txn_id,
                        target_file=path,
                        goal=results[path].goal,
                        iterations=0,
                        total_time=0,
                        error=f"Transaction commit failed: {error}",
                    )

            return results

        except Exception as e:
            await self._multi_file_orchestrator.abort_transaction()
            raise

    def get_status(self) -> Dict[str, Any]:
        """Get enhanced status."""
        base_status = super().get_status()
        base_status["enhanced"] = {
            "session_id": self._session_memory.session_id,
            "session_duration_seconds": self._session_memory.duration_seconds,
            "require_approval": self._require_approval,
            "pending_previews": len(self._diff_preview_engine.get_pending_previews()),
        }
        return base_status


# =============================================================================
# ENHANCED GLOBAL INSTANCE
# =============================================================================

_enhanced_engine: Optional[EnhancedSelfImprovement] = None


def get_enhanced_self_improvement() -> EnhancedSelfImprovement:
    """Get the enhanced self-improvement engine with Claude Code-like behaviors."""
    global _enhanced_engine
    if _enhanced_engine is None:
        _enhanced_engine = EnhancedSelfImprovement()
    return _enhanced_engine


async def execute_with_preview(
    target: Union[str, Path],
    goal: str,
    test_command: Optional[str] = None,
    context: Optional[str] = None,
    max_iterations: int = 5,
    require_approval: bool = False,
) -> ImprovementResult:
    """
    Execute improvement with diff preview - Claude Code-like interface.

    Example:
        result = await execute_with_preview(
            target="backend/core/utils.py",
            goal="Fix the race condition",
            require_approval=True  # Show diff and wait for approval
        )
    """
    engine = get_enhanced_self_improvement()
    if not engine._running:
        await engine.initialize()

    return await engine.execute_with_preview(
        target=target,
        goal=goal,
        test_command=test_command,
        context=context,
        max_iterations=max_iterations,
        require_approval=require_approval,
    )


async def execute_multi_file(
    files_and_goals: List[Tuple[Union[str, Path], str]],
    shared_context: Optional[str] = None,
    require_approval: bool = True,
) -> Dict[str, ImprovementResult]:
    """
    Execute improvements on multiple files atomically.

    Example:
        results = await execute_multi_file([
            ("backend/api/routes.py", "Add rate limiting"),
            ("backend/core/limiter.py", "Implement rate limiter"),
            ("tests/test_limiter.py", "Add rate limiter tests"),
        ])
    """
    engine = get_enhanced_self_improvement()
    if not engine._running:
        await engine.initialize()

    return await engine.execute_multi_file_improvement(
        files_and_goals=files_and_goals,
        shared_context=shared_context,
        require_approval=require_approval,
    )


# =============================================================================
# v4.0: AUTONOMOUS SELF-PROGRAMMING - THE MISSING 20%
# =============================================================================
# These components enable TRUE autonomous decision-making:
# - GoalDecompositionEngine: Breaks "Make yourself faster" into actionable tasks
# - TechnicalDebtDetector: Finds issues without being told
# - AutonomousSelfRefinementLoop: Runs continuously without prompts
# - DualAgentSystem: Architect generates, Reviewer critiques
# - CodeMemoryRAG: Oracle (graph) + ChromaDB (semantic) fusion
# - SystemFeedbackLoop: Hardware metrics trigger code improvements
# - AutoTestGenerator: Generates tests for untested existing code
# =============================================================================


class SubTaskPriority(Enum):
    """Priority levels for decomposed sub-tasks."""
    CRITICAL = 1      # Blocking issues, security vulnerabilities
    HIGH = 2          # Performance issues, missing tests
    MEDIUM = 3        # Code smells, minor improvements
    LOW = 4           # Style issues, documentation
    DEFERRED = 5      # Future improvements


class SubTaskType(Enum):
    """Types of sub-tasks from goal decomposition."""
    ANALYZE = "analyze"           # Investigate/profile code
    IMPLEMENT = "implement"       # Write new code
    REFACTOR = "refactor"         # Restructure existing code
    FIX = "fix"                   # Fix bugs/issues
    TEST = "test"                 # Write/run tests
    DOCUMENT = "document"         # Add documentation
    OPTIMIZE = "optimize"         # Performance improvements
    VALIDATE = "validate"         # Verify changes work
    BENCHMARK = "benchmark"       # Measure performance
    REVIEW = "review"             # Code review


class TechnicalDebtCategory(Enum):
    """Categories of technical debt."""
    COMPLEXITY = "complexity"         # High cyclomatic/cognitive complexity
    DUPLICATION = "duplication"       # Code duplication
    DEAD_CODE = "dead_code"           # Unused code
    CIRCULAR_DEPS = "circular_deps"   # Circular dependencies
    MISSING_TESTS = "missing_tests"   # Insufficient test coverage
    SECURITY = "security"             # Security vulnerabilities
    PERFORMANCE = "performance"       # Performance bottlenecks
    TYPE_SAFETY = "type_safety"       # Missing type hints
    ERROR_HANDLING = "error_handling" # Poor error handling
    DEPRECATED = "deprecated"         # Deprecated API usage
    COUPLING = "coupling"             # High coupling between modules


@dataclass
class SubTask:
    """
    A decomposed sub-task from a high-level goal.

    This is the atomic unit of work that can be assigned to the
    AgenticLoopOrchestrator for execution.
    """
    id: str
    description: str
    task_type: SubTaskType
    priority: SubTaskPriority
    target_files: List[str]
    dependencies: List[str] = field(default_factory=list)  # IDs of tasks that must complete first
    success_criteria: str = ""
    estimated_complexity: str = "medium"  # trivial, easy, medium, hard, expert
    context: Dict[str, Any] = field(default_factory=dict)
    parent_goal: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"  # pending, in_progress, completed, failed, blocked
    result: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "description": self.description,
            "task_type": self.task_type.value,
            "priority": self.priority.value,
            "target_files": self.target_files,
            "dependencies": self.dependencies,
            "success_criteria": self.success_criteria,
            "estimated_complexity": self.estimated_complexity,
            "context": self.context,
            "parent_goal": self.parent_goal,
            "status": self.status,
        }


@dataclass
class TechnicalDebtItem:
    """
    A single instance of technical debt detected in the codebase.
    """
    id: str
    category: TechnicalDebtCategory
    file_path: str
    line_start: int
    line_end: int
    severity: float  # 0.0 - 1.0
    description: str
    code_snippet: str = ""
    suggested_fix: str = ""
    estimated_effort: str = "medium"  # trivial, easy, medium, hard, expert
    detected_at: float = field(default_factory=time.time)
    metrics: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "category": self.category.value,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "severity": self.severity,
            "description": self.description,
            "suggested_fix": self.suggested_fix,
            "estimated_effort": self.estimated_effort,
        }


@dataclass
class DecompositionResult:
    """Result of goal decomposition."""
    original_goal: str
    sub_tasks: List[SubTask]
    execution_order: List[str]  # Task IDs in order
    total_estimated_complexity: str
    reasoning: str
    dependencies_graph: Dict[str, List[str]]  # task_id -> [dependency_ids]
    created_at: float = field(default_factory=time.time)


@dataclass
class CodeMemoryResult:
    """Result from CodeMemoryRAG search."""
    query: str
    graph_results: List[Dict[str, Any]]  # From Oracle
    semantic_results: List[Dict[str, Any]]  # From ChromaDB
    merged_results: List[Dict[str, Any]]  # Combined and ranked
    relevance_scores: Dict[str, float]
    total_results: int


class GoalDecompositionEngine:
    """
    v4.0: Autonomous goal breakdown engine using LLM-powered analysis.

    This is the CRITICAL missing piece - the ability to take a high-level goal
    like "Make yourself faster" and decompose it into specific, actionable
    sub-tasks that can be executed by the AgenticLoopOrchestrator.

    The decomposition process:
    1. Analyze the goal using LLM to understand intent
    2. Query Oracle for relevant code structure
    3. Identify affected files and dependencies
    4. Generate ordered sub-tasks with dependencies
    5. Estimate complexity and priority for each

    Example:
        engine = GoalDecompositionEngine(oracle, llm_client)
        result = await engine.decompose(
            "Make the voice authentication faster",
            context={"codebase": "jarvis"}
        )
        # Returns: [
        #   SubTask("Profile voice_auth.py to find bottlenecks", ANALYZE, HIGH),
        #   SubTask("Cache speaker embeddings", IMPLEMENT, HIGH),
        #   SubTask("Optimize cosine similarity calculation", OPTIMIZE, MEDIUM),
        #   SubTask("Add benchmark tests", TEST, MEDIUM),
        # ]
    """

    # System prompt for goal decomposition
    DECOMPOSITION_SYSTEM_PROMPT = """You are an expert software architect specializing in breaking down complex improvement goals into actionable sub-tasks.

Your job is to analyze high-level goals and decompose them into specific, executable tasks that can be performed by an AI coding agent.

For each sub-task, you must specify:
1. A clear, actionable description
2. The type of task (analyze, implement, refactor, fix, test, document, optimize, validate, benchmark, review)
3. Priority (critical, high, medium, low)
4. Target files that will be affected
5. Dependencies on other tasks (which tasks must complete first)
6. Success criteria (how to verify the task is complete)
7. Estimated complexity (trivial, easy, medium, hard, expert)

Guidelines:
- Break goals into 3-10 specific sub-tasks
- Always start with analysis/profiling for optimization goals
- Always end with validation/testing tasks
- Consider dependencies between tasks (order matters)
- Be specific about file paths when possible
- Identify potential risks and blockers

Output your response as valid JSON with this structure:
{
    "reasoning": "Your analysis of the goal and approach",
    "sub_tasks": [
        {
            "description": "Task description",
            "task_type": "analyze|implement|refactor|fix|test|document|optimize|validate|benchmark|review",
            "priority": "critical|high|medium|low",
            "target_files": ["file1.py", "file2.py"],
            "dependencies": [],
            "success_criteria": "How to verify completion",
            "estimated_complexity": "trivial|easy|medium|hard|expert"
        }
    ],
    "execution_order": ["task_0", "task_1", ...],
    "total_complexity": "easy|medium|hard|expert"
}"""

    def __init__(
        self,
        oracle: Optional[Any] = None,
        llm_client: Optional[Any] = None,
        learning_db: Optional[Any] = None,
    ):
        """
        Initialize the goal decomposition engine.

        Args:
            oracle: The Oracle codebase knowledge graph
            llm_client: LLM client for goal analysis (Prime or Claude)
            learning_db: Learning database for pattern storage
        """
        self._oracle = oracle
        self._llm_client = llm_client
        self._learning_db = learning_db
        self._lock = asyncio.Lock()
        self._cache: Dict[str, DecompositionResult] = {}
        self._decomposition_history: List[DecompositionResult] = []

        # Configuration from environment
        self._max_sub_tasks = int(os.getenv("GOAL_DECOMP_MAX_TASKS", "10"))
        self._cache_ttl = int(os.getenv("GOAL_DECOMP_CACHE_TTL", "3600"))
        self._min_sub_tasks = int(os.getenv("GOAL_DECOMP_MIN_TASKS", "2"))

        logger.info("GoalDecompositionEngine initialized")

    async def set_oracle(self, oracle: Any) -> None:
        """Set the Oracle instance."""
        self._oracle = oracle

    async def set_llm_client(self, llm_client: Any) -> None:
        """Set the LLM client."""
        self._llm_client = llm_client

    async def decompose(
        self,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
        target_files: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> DecompositionResult:
        """
        Decompose a high-level goal into actionable sub-tasks.

        Args:
            goal: The high-level goal to decompose
            context: Additional context (codebase info, constraints, etc.)
            target_files: Optional list of specific files to focus on
            force_refresh: Bypass cache and regenerate

        Returns:
            DecompositionResult with ordered sub-tasks
        """
        # Check cache
        cache_key = hashlib.sha256(f"{goal}:{json.dumps(context or {})}".encode()).hexdigest()[:16]
        if not force_refresh and cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached.created_at < self._cache_ttl:
                logger.debug(f"Using cached decomposition for goal: {goal[:50]}...")
                return cached

        async with self._lock:
            # Gather context from Oracle
            oracle_context = await self._gather_oracle_context(goal, target_files)

            # Query learning DB for similar past decompositions
            historical_patterns = await self._get_historical_patterns(goal)

            # Build the prompt
            prompt = await self._build_decomposition_prompt(
                goal, context or {}, oracle_context, historical_patterns, target_files
            )

            # Call LLM for decomposition
            response = await self._call_llm(prompt)

            # Parse response into sub-tasks
            result = await self._parse_decomposition_response(goal, response)

            # Cache and store
            self._cache[cache_key] = result
            self._decomposition_history.append(result)

            # Record in learning DB
            if self._learning_db:
                await self._record_decomposition(result)

            logger.info(f"Decomposed goal '{goal[:50]}...' into {len(result.sub_tasks)} sub-tasks")
            return result

    async def _gather_oracle_context(
        self,
        goal: str,
        target_files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Gather relevant codebase context from Oracle."""
        if not self._oracle:
            return {"available": False}

        context = {"available": True}

        try:
            # Extract keywords from goal for file search
            keywords = self._extract_keywords(goal)

            # Find relevant files
            relevant_files = []
            for keyword in keywords:
                try:
                    nodes = await asyncio.to_thread(
                        lambda k=keyword: self._oracle.find_nodes_by_name(k, fuzzy=True)
                        if hasattr(self._oracle, 'find_nodes_by_name') else []
                    )
                    relevant_files.extend([str(n) for n in nodes[:5]])
                except Exception:
                    pass

            context["relevant_files"] = list(set(relevant_files))[:20]

            # Get dead code if optimization goal
            if any(kw in goal.lower() for kw in ["optimize", "performance", "speed", "fast"]):
                try:
                    dead_code = await asyncio.to_thread(
                        lambda: self._oracle.get_dead_code()
                        if hasattr(self._oracle, 'get_dead_code') else []
                    )
                    context["dead_code"] = [str(d) for d in list(dead_code)[:10]]
                except Exception:
                    pass

            # Get circular dependencies if refactor goal
            if any(kw in goal.lower() for kw in ["refactor", "clean", "structure", "architecture"]):
                try:
                    cycles = await asyncio.to_thread(
                        lambda: self._oracle.get_circular_dependencies()
                        if hasattr(self._oracle, 'get_circular_dependencies') else []
                    )
                    context["circular_deps"] = [str(c) for c in list(cycles)[:5]]
                except Exception:
                    pass

            # Get blast radius for target files
            if target_files:
                blast_radii = {}
                for file in target_files[:3]:
                    try:
                        radius = await asyncio.to_thread(
                            lambda f=file: self._oracle.get_blast_radius(f)
                            if hasattr(self._oracle, 'get_blast_radius') else None
                        )
                        if radius:
                            blast_radii[file] = {
                                "directly_affected": radius.directly_affected[:10]
                                if hasattr(radius, 'directly_affected') else [],
                                "risk_level": getattr(radius, 'risk_level', 'unknown'),
                            }
                    except Exception:
                        pass
                context["blast_radii"] = blast_radii

        except Exception as e:
            logger.warning(f"Error gathering Oracle context: {e}")
            context["error"] = str(e)

        return context

    def _extract_keywords(self, goal: str) -> List[str]:
        """Extract relevant keywords from goal for search."""
        # Remove common words and extract meaningful terms
        stop_words = {
            "the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or",
            "is", "are", "be", "been", "being", "have", "has", "had", "do", "does",
            "did", "will", "would", "could", "should", "may", "might", "must",
            "make", "it", "this", "that", "these", "those", "i", "you", "we",
            "more", "better", "improve", "fix", "add", "create", "update",
        }

        words = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', goal.lower())
        keywords = [w for w in words if w not in stop_words and len(w) > 2]

        # Also extract potential class/function names (CamelCase or snake_case)
        camel_case = re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)*', goal)
        snake_case = re.findall(r'[a-z]+(?:_[a-z]+)+', goal)

        return list(set(keywords + camel_case + snake_case))[:10]

    async def _get_historical_patterns(self, goal: str) -> List[Dict[str, Any]]:
        """Get similar historical decompositions from learning DB."""
        if not self._learning_db:
            return []

        try:
            # Query for similar past decompositions
            if hasattr(self._learning_db, 'query_patterns'):
                patterns = await self._learning_db.query_patterns(
                    category="goal_decomposition",
                    limit=3,
                )
                return patterns
        except Exception as e:
            logger.debug(f"Error getting historical patterns: {e}")

        return []

    async def _build_decomposition_prompt(
        self,
        goal: str,
        context: Dict[str, Any],
        oracle_context: Dict[str, Any],
        historical_patterns: List[Dict[str, Any]],
        target_files: Optional[List[str]],
    ) -> str:
        """Build the prompt for LLM decomposition."""
        prompt_parts = [
            f"# Goal to Decompose\n{goal}\n",
        ]

        if context:
            prompt_parts.append(f"# Additional Context\n{json.dumps(context, indent=2)}\n")

        if oracle_context.get("available"):
            prompt_parts.append("# Codebase Analysis from Oracle")
            if oracle_context.get("relevant_files"):
                prompt_parts.append(f"Relevant files: {', '.join(oracle_context['relevant_files'][:10])}")
            if oracle_context.get("dead_code"):
                prompt_parts.append(f"Dead code detected: {', '.join(oracle_context['dead_code'][:5])}")
            if oracle_context.get("circular_deps"):
                prompt_parts.append(f"Circular dependencies: {', '.join(str(c) for c in oracle_context['circular_deps'][:3])}")
            if oracle_context.get("blast_radii"):
                prompt_parts.append(f"Blast radius info: {json.dumps(oracle_context['blast_radii'], indent=2)}")
            prompt_parts.append("")

        if target_files:
            prompt_parts.append(f"# Target Files to Focus On\n{', '.join(target_files)}\n")

        if historical_patterns:
            prompt_parts.append("# Similar Past Decompositions")
            for pattern in historical_patterns[:2]:
                if isinstance(pattern, dict):
                    prompt_parts.append(f"- Goal: {pattern.get('goal', 'N/A')[:100]}")
                    prompt_parts.append(f"  Tasks: {pattern.get('task_count', 'N/A')}")
            prompt_parts.append("")

        prompt_parts.append(
            "Please decompose this goal into specific, actionable sub-tasks. "
            "Return valid JSON as specified in the system prompt."
        )

        return "\n".join(prompt_parts)

    async def _call_llm(self, prompt: str) -> str:
        """Call the LLM for decomposition."""
        if not self._llm_client:
            # Fallback to basic decomposition without LLM
            return await self._basic_decomposition_fallback(prompt)

        try:
            # Try different LLM client interfaces
            if hasattr(self._llm_client, 'complete'):
                response = await self._llm_client.complete(
                    system=self.DECOMPOSITION_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                    temperature=0.3,
                )
                return response[0] if isinstance(response, tuple) else response
            elif hasattr(self._llm_client, 'generate'):
                response = await self._llm_client.generate(
                    prompt=f"{self.DECOMPOSITION_SYSTEM_PROMPT}\n\n{prompt}",
                    max_tokens=2000,
                )
                return response
            elif hasattr(self._llm_client, 'chat'):
                response = await self._llm_client.chat(
                    messages=[
                        {"role": "system", "content": self.DECOMPOSITION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=2000,
                )
                return response.get("content", "") if isinstance(response, dict) else str(response)
            else:
                logger.warning("Unknown LLM client interface, using fallback")
                return await self._basic_decomposition_fallback(prompt)
        except Exception as e:
            logger.error(f"LLM call failed: {e}, using fallback")
            return await self._basic_decomposition_fallback(prompt)

    async def _basic_decomposition_fallback(self, prompt: str) -> str:
        """Basic rule-based decomposition when LLM is unavailable."""
        goal = prompt.split("\n")[1] if "\n" in prompt else prompt
        goal_lower = goal.lower()

        sub_tasks = []

        # Determine task types based on goal keywords
        if any(kw in goal_lower for kw in ["optimize", "performance", "speed", "fast"]):
            sub_tasks = [
                {"description": "Profile code to identify bottlenecks", "task_type": "analyze", "priority": "high", "target_files": [], "dependencies": [], "success_criteria": "Profiling report generated", "estimated_complexity": "easy"},
                {"description": "Implement optimizations based on profiling", "task_type": "optimize", "priority": "high", "target_files": [], "dependencies": ["task_0"], "success_criteria": "Optimizations applied", "estimated_complexity": "medium"},
                {"description": "Run benchmarks to verify improvements", "task_type": "benchmark", "priority": "medium", "target_files": [], "dependencies": ["task_1"], "success_criteria": "Performance improved", "estimated_complexity": "easy"},
            ]
        elif any(kw in goal_lower for kw in ["fix", "bug", "error", "issue"]):
            sub_tasks = [
                {"description": "Analyze error and identify root cause", "task_type": "analyze", "priority": "critical", "target_files": [], "dependencies": [], "success_criteria": "Root cause identified", "estimated_complexity": "medium"},
                {"description": "Implement fix for the issue", "task_type": "fix", "priority": "critical", "target_files": [], "dependencies": ["task_0"], "success_criteria": "Fix implemented", "estimated_complexity": "medium"},
                {"description": "Add tests to prevent regression", "task_type": "test", "priority": "high", "target_files": [], "dependencies": ["task_1"], "success_criteria": "Tests pass", "estimated_complexity": "easy"},
            ]
        elif any(kw in goal_lower for kw in ["refactor", "clean", "improve"]):
            sub_tasks = [
                {"description": "Analyze code structure and identify improvements", "task_type": "analyze", "priority": "high", "target_files": [], "dependencies": [], "success_criteria": "Analysis complete", "estimated_complexity": "easy"},
                {"description": "Refactor code to improve structure", "task_type": "refactor", "priority": "high", "target_files": [], "dependencies": ["task_0"], "success_criteria": "Code refactored", "estimated_complexity": "medium"},
                {"description": "Validate refactoring with existing tests", "task_type": "validate", "priority": "medium", "target_files": [], "dependencies": ["task_1"], "success_criteria": "All tests pass", "estimated_complexity": "easy"},
            ]
        else:
            sub_tasks = [
                {"description": f"Analyze requirements for: {goal}", "task_type": "analyze", "priority": "high", "target_files": [], "dependencies": [], "success_criteria": "Requirements understood", "estimated_complexity": "easy"},
                {"description": "Implement the requested changes", "task_type": "implement", "priority": "high", "target_files": [], "dependencies": ["task_0"], "success_criteria": "Implementation complete", "estimated_complexity": "medium"},
                {"description": "Test and validate changes", "task_type": "validate", "priority": "medium", "target_files": [], "dependencies": ["task_1"], "success_criteria": "Validation passed", "estimated_complexity": "easy"},
            ]

        return json.dumps({
            "reasoning": "Basic rule-based decomposition (LLM unavailable)",
            "sub_tasks": sub_tasks,
            "execution_order": [f"task_{i}" for i in range(len(sub_tasks))],
            "total_complexity": "medium",
        })

    async def _parse_decomposition_response(
        self,
        goal: str,
        response: str,
    ) -> DecompositionResult:
        """Parse the LLM response into a DecompositionResult."""
        try:
            # Extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', response)
            if not json_match:
                raise ValueError("No JSON found in response")

            data = json.loads(json_match.group())

            # Build sub-tasks
            sub_tasks = []
            for i, task_data in enumerate(data.get("sub_tasks", [])):
                task_id = f"task_{i}"

                # Parse enums safely
                try:
                    task_type = SubTaskType(task_data.get("task_type", "implement"))
                except ValueError:
                    task_type = SubTaskType.IMPLEMENT

                try:
                    priority_str = task_data.get("priority", "medium").upper()
                    priority = SubTaskPriority[priority_str] if priority_str in SubTaskPriority.__members__ else SubTaskPriority.MEDIUM
                except (KeyError, ValueError):
                    priority = SubTaskPriority.MEDIUM

                sub_task = SubTask(
                    id=task_id,
                    description=task_data.get("description", f"Task {i}"),
                    task_type=task_type,
                    priority=priority,
                    target_files=task_data.get("target_files", []),
                    dependencies=task_data.get("dependencies", []),
                    success_criteria=task_data.get("success_criteria", ""),
                    estimated_complexity=task_data.get("estimated_complexity", "medium"),
                    parent_goal=goal,
                )
                sub_tasks.append(sub_task)

            # Build dependency graph
            deps_graph = {task.id: task.dependencies for task in sub_tasks}

            return DecompositionResult(
                original_goal=goal,
                sub_tasks=sub_tasks,
                execution_order=data.get("execution_order", [t.id for t in sub_tasks]),
                total_estimated_complexity=data.get("total_complexity", "medium"),
                reasoning=data.get("reasoning", ""),
                dependencies_graph=deps_graph,
            )

        except Exception as e:
            logger.error(f"Failed to parse decomposition response: {e}")
            # Return minimal result
            return DecompositionResult(
                original_goal=goal,
                sub_tasks=[SubTask(
                    id="task_0",
                    description=goal,
                    task_type=SubTaskType.IMPLEMENT,
                    priority=SubTaskPriority.HIGH,
                    target_files=[],
                    parent_goal=goal,
                )],
                execution_order=["task_0"],
                total_estimated_complexity="medium",
                reasoning=f"Parse error: {e}",
                dependencies_graph={"task_0": []},
            )

    async def _record_decomposition(self, result: DecompositionResult) -> None:
        """Record decomposition in learning database."""
        if not self._learning_db:
            return

        try:
            if hasattr(self._learning_db, 'record_experience'):
                await self._learning_db.record_experience(
                    category="goal_decomposition",
                    data={
                        "goal": result.original_goal,
                        "task_count": len(result.sub_tasks),
                        "complexity": result.total_estimated_complexity,
                        "task_types": [t.task_type.value for t in result.sub_tasks],
                    }
                )
        except Exception as e:
            logger.debug(f"Error recording decomposition: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get engine status."""
        return {
            "cache_size": len(self._cache),
            "history_size": len(self._decomposition_history),
            "oracle_connected": self._oracle is not None,
            "llm_connected": self._llm_client is not None,
            "learning_db_connected": self._learning_db is not None,
        }


class TechnicalDebtDetector:
    """
    v4.0: Autonomous codebase scanner for technical debt detection.

    This component enables JARVIS to find problems WITHOUT being told -
    it scans the codebase and identifies issues that need attention.

    Detection categories:
    - High complexity functions (cyclomatic, cognitive)
    - Code duplication
    - Dead/unused code
    - Circular dependencies
    - Missing test coverage
    - Security vulnerabilities
    - Performance bottlenecks
    - Type safety issues
    - Poor error handling
    - Deprecated API usage

    Integration:
    - Uses Oracle for structural analysis
    - Uses AST for complexity metrics
    - Uses pattern matching for security/performance issues
    - Prioritizes issues by severity and effort
    """

    # Complexity thresholds (configurable via environment)
    CYCLOMATIC_THRESHOLD = int(os.getenv("DEBT_CYCLOMATIC_THRESHOLD", "10"))
    COGNITIVE_THRESHOLD = int(os.getenv("DEBT_COGNITIVE_THRESHOLD", "15"))
    LINE_COUNT_THRESHOLD = int(os.getenv("DEBT_LINE_THRESHOLD", "200"))
    NESTING_THRESHOLD = int(os.getenv("DEBT_NESTING_THRESHOLD", "4"))

    # Security patterns to detect
    SECURITY_PATTERNS = [
        (r'eval\s*\(', "eval() usage - potential code injection", 0.9),
        (r'exec\s*\(', "exec() usage - potential code injection", 0.9),
        (r'__import__\s*\(', "Dynamic import - potential security risk", 0.7),
        (r'subprocess\..*shell\s*=\s*True', "Shell=True in subprocess - command injection risk", 0.85),
        (r'pickle\.loads?\s*\(', "Pickle usage - potential arbitrary code execution", 0.8),
        (r'yaml\.load\s*\([^,]+\)(?!.*Loader)', "yaml.load without Loader - arbitrary code execution", 0.85),
        (r'request\.args\.get|request\.form\.get', "Unsanitized user input", 0.6),
        (r'os\.system\s*\(', "os.system usage - prefer subprocess", 0.7),
        (r'\.format\s*\([^)]*request', "Format string with user input - potential injection", 0.75),
        (r'f["\'][^"\']*\{[^}]*request', "F-string with user input - potential injection", 0.75),
    ]

    # Performance patterns to detect
    PERFORMANCE_PATTERNS = [
        (r'for\s+\w+\s+in\s+range\(len\(', "range(len()) - use enumerate instead", 0.4),
        (r'\.append\([^)]+\)\s*$.*for\s+', "Append in loop - consider list comprehension", 0.3),
        (r'\+\s*=\s*["\']', "String concatenation in loop - use join()", 0.5),
        (r'time\.sleep\s*\(\s*[0-9]+\s*\)', "Blocking sleep - consider async sleep", 0.4),
        (r'open\s*\([^)]+\)(?!.*with)', "File open without context manager", 0.6),
        (r'except\s*:\s*$', "Bare except clause - catches SystemExit", 0.65),
        (r'except\s+Exception\s*:\s*$', "Broad exception catch", 0.5),
        (r'global\s+\w+', "Global variable usage - potential side effects", 0.4),
    ]

    def __init__(
        self,
        oracle: Optional[Any] = None,
        project_root: Optional[Path] = None,
    ):
        """
        Initialize technical debt detector.

        Args:
            oracle: The Oracle codebase knowledge graph
            project_root: Root directory of the project to scan
        """
        self._oracle = oracle
        self._project_root = project_root or Path(os.getenv(
            "JARVIS_PATH",
            Path.home() / "Documents/repos/JARVIS-AI-Agent"
        ))
        self._lock = asyncio.Lock()
        self._scan_results: List[TechnicalDebtItem] = []
        self._last_scan: Optional[float] = None
        self._scan_interval = int(os.getenv("DEBT_SCAN_INTERVAL", "3600"))  # 1 hour

        logger.info(f"TechnicalDebtDetector initialized for {self._project_root}")

    async def set_oracle(self, oracle: Any) -> None:
        """Set the Oracle instance."""
        self._oracle = oracle

    async def scan_codebase(
        self,
        target_dirs: Optional[List[str]] = None,
        categories: Optional[List[TechnicalDebtCategory]] = None,
        max_issues: int = 100,
        force_refresh: bool = False,
    ) -> List[TechnicalDebtItem]:
        """
        Scan the codebase for technical debt.

        Args:
            target_dirs: Specific directories to scan (default: entire project)
            categories: Specific categories to scan for (default: all)
            max_issues: Maximum number of issues to return
            force_refresh: Force rescan even if within scan interval

        Returns:
            List of detected technical debt items, sorted by severity
        """
        # Check if we should use cached results
        if not force_refresh and self._last_scan:
            if time.time() - self._last_scan < self._scan_interval:
                logger.debug("Using cached scan results")
                return self._filter_results(categories, max_issues)

        async with self._lock:
            self._scan_results = []

            # Determine directories to scan
            scan_dirs = []
            if target_dirs:
                scan_dirs = [self._project_root / d for d in target_dirs]
            else:
                scan_dirs = [self._project_root / "backend"]

            # Get all Python files
            python_files = []
            for scan_dir in scan_dirs:
                if scan_dir.exists():
                    python_files.extend(scan_dir.rglob("*.py"))

            # Filter out test files and cache
            python_files = [
                f for f in python_files
                if "__pycache__" not in str(f)
                and ".venv" not in str(f)
                and "node_modules" not in str(f)
            ]

            logger.info(f"Scanning {len(python_files)} Python files for technical debt...")

            # Scan in parallel
            tasks = []
            for file_path in python_files[:200]:  # Limit to avoid overwhelming
                tasks.append(self._scan_file(file_path, categories))

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    self._scan_results.extend(result)
                elif isinstance(result, Exception):
                    logger.debug(f"Scan error: {result}")

            # Add Oracle-based detections
            if self._oracle:
                await self._add_oracle_detections(categories)

            # Sort by severity
            self._scan_results.sort(key=lambda x: x.severity, reverse=True)
            self._last_scan = time.time()

            logger.info(f"Technical debt scan complete: {len(self._scan_results)} issues found")
            return self._filter_results(categories, max_issues)

    async def _scan_file(
        self,
        file_path: Path,
        categories: Optional[List[TechnicalDebtCategory]],
    ) -> List[TechnicalDebtItem]:
        """Scan a single file for technical debt."""
        items = []

        try:
            content = await asyncio.to_thread(file_path.read_text, encoding="utf-8")
            lines = content.splitlines()

            # Parse AST
            try:
                tree = ast.parse(content)
            except SyntaxError:
                return items

            # Check complexity
            if not categories or TechnicalDebtCategory.COMPLEXITY in categories:
                items.extend(await self._check_complexity(file_path, tree, lines))

            # Check security
            if not categories or TechnicalDebtCategory.SECURITY in categories:
                items.extend(await self._check_security(file_path, content, lines))

            # Check performance
            if not categories or TechnicalDebtCategory.PERFORMANCE in categories:
                items.extend(await self._check_performance(file_path, content, lines))

            # Check error handling
            if not categories or TechnicalDebtCategory.ERROR_HANDLING in categories:
                items.extend(await self._check_error_handling(file_path, tree, lines))

            # Check type safety
            if not categories or TechnicalDebtCategory.TYPE_SAFETY in categories:
                items.extend(await self._check_type_safety(file_path, tree, lines))

        except Exception as e:
            logger.debug(f"Error scanning {file_path}: {e}")

        return items

    async def _check_complexity(
        self,
        file_path: Path,
        tree: ast.AST,
        lines: List[str],
    ) -> List[TechnicalDebtItem]:
        """Check for complexity issues."""
        items = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Calculate cyclomatic complexity
                cyclomatic = self._calculate_cyclomatic_complexity(node)

                # Calculate cognitive complexity
                cognitive = self._calculate_cognitive_complexity(node)

                # Check line count
                end_line = getattr(node, 'end_lineno', node.lineno + 10)
                line_count = end_line - node.lineno

                # Check nesting depth
                max_nesting = self._calculate_max_nesting(node)

                severity = 0.0
                issues = []

                if cyclomatic > self.CYCLOMATIC_THRESHOLD:
                    severity = max(severity, min(0.9, cyclomatic / 30))
                    issues.append(f"cyclomatic={cyclomatic}")

                if cognitive > self.COGNITIVE_THRESHOLD:
                    severity = max(severity, min(0.85, cognitive / 40))
                    issues.append(f"cognitive={cognitive}")

                if line_count > self.LINE_COUNT_THRESHOLD:
                    severity = max(severity, min(0.7, line_count / 500))
                    issues.append(f"lines={line_count}")

                if max_nesting > self.NESTING_THRESHOLD:
                    severity = max(severity, min(0.6, max_nesting / 8))
                    issues.append(f"nesting={max_nesting}")

                if severity > 0:
                    items.append(TechnicalDebtItem(
                        id=f"complexity_{uuid.uuid4().hex[:8]}",
                        category=TechnicalDebtCategory.COMPLEXITY,
                        file_path=str(file_path),
                        line_start=node.lineno,
                        line_end=end_line,
                        severity=severity,
                        description=f"High complexity in '{node.name}': {', '.join(issues)}",
                        code_snippet=lines[node.lineno - 1] if node.lineno <= len(lines) else "",
                        suggested_fix="Consider breaking this function into smaller, focused functions",
                        estimated_effort="medium" if severity < 0.7 else "hard",
                        metrics={
                            "cyclomatic": cyclomatic,
                            "cognitive": cognitive,
                            "line_count": line_count,
                            "max_nesting": max_nesting,
                        },
                    ))

        return items

    def _calculate_cyclomatic_complexity(self, node: ast.AST) -> int:
        """Calculate cyclomatic complexity of a function."""
        complexity = 1  # Base complexity

        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                complexity += 1
            elif isinstance(child, ast.ExceptHandler):
                complexity += 1
            elif isinstance(child, (ast.And, ast.Or)):
                complexity += 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
                if child.ifs:
                    complexity += len(child.ifs)
            elif isinstance(child, ast.Match):
                complexity += len(child.cases) - 1

        return complexity

    def _calculate_cognitive_complexity(self, node: ast.AST, depth: int = 0) -> int:
        """Calculate cognitive complexity of a function."""
        complexity = 0

        for child in ast.iter_child_nodes(node):
            increment = 0
            nesting_increment = 0

            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                increment = 1
                nesting_increment = depth
            elif isinstance(child, ast.ExceptHandler):
                increment = 1
                nesting_increment = depth
            elif isinstance(child, (ast.And, ast.Or)):
                increment = 1
            elif isinstance(child, ast.Lambda):
                increment = 1

            complexity += increment + nesting_increment

            # Recurse with increased depth for nesting structures
            new_depth = depth + 1 if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Try)) else depth
            complexity += self._calculate_cognitive_complexity(child, new_depth)

        return complexity

    def _calculate_max_nesting(self, node: ast.AST, depth: int = 0) -> int:
        """Calculate maximum nesting depth."""
        max_depth = depth

        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.Try, ast.With, ast.AsyncWith)):
                child_depth = self._calculate_max_nesting(child, depth + 1)
                max_depth = max(max_depth, child_depth)
            else:
                child_depth = self._calculate_max_nesting(child, depth)
                max_depth = max(max_depth, child_depth)

        return max_depth

    async def _check_security(
        self,
        file_path: Path,
        content: str,
        lines: List[str],
    ) -> List[TechnicalDebtItem]:
        """Check for security issues."""
        items = []

        for pattern, description, severity in self.SECURITY_PATTERNS:
            for match in re.finditer(pattern, content, re.MULTILINE):
                line_num = content[:match.start()].count('\n') + 1
                items.append(TechnicalDebtItem(
                    id=f"security_{uuid.uuid4().hex[:8]}",
                    category=TechnicalDebtCategory.SECURITY,
                    file_path=str(file_path),
                    line_start=line_num,
                    line_end=line_num,
                    severity=severity,
                    description=description,
                    code_snippet=lines[line_num - 1].strip() if line_num <= len(lines) else "",
                    suggested_fix="Review and apply security best practices",
                    estimated_effort="medium",
                ))

        return items

    async def _check_performance(
        self,
        file_path: Path,
        content: str,
        lines: List[str],
    ) -> List[TechnicalDebtItem]:
        """Check for performance issues."""
        items = []

        for pattern, description, severity in self.PERFORMANCE_PATTERNS:
            for match in re.finditer(pattern, content, re.MULTILINE):
                line_num = content[:match.start()].count('\n') + 1
                items.append(TechnicalDebtItem(
                    id=f"perf_{uuid.uuid4().hex[:8]}",
                    category=TechnicalDebtCategory.PERFORMANCE,
                    file_path=str(file_path),
                    line_start=line_num,
                    line_end=line_num,
                    severity=severity,
                    description=description,
                    code_snippet=lines[line_num - 1].strip() if line_num <= len(lines) else "",
                    suggested_fix="Apply performance optimization",
                    estimated_effort="easy",
                ))

        return items

    async def _check_error_handling(
        self,
        file_path: Path,
        tree: ast.AST,
        lines: List[str],
    ) -> List[TechnicalDebtItem]:
        """Check for error handling issues."""
        items = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                # Bare except
                if node.type is None:
                    items.append(TechnicalDebtItem(
                        id=f"error_{uuid.uuid4().hex[:8]}",
                        category=TechnicalDebtCategory.ERROR_HANDLING,
                        file_path=str(file_path),
                        line_start=node.lineno,
                        line_end=node.lineno,
                        severity=0.7,
                        description="Bare except clause catches all exceptions including SystemExit",
                        code_snippet=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
                        suggested_fix="Specify the exception type to catch",
                        estimated_effort="easy",
                    ))
                # Except Exception with pass
                elif isinstance(node.type, ast.Name) and node.type.id == "Exception":
                    if node.body and isinstance(node.body[0], ast.Pass):
                        items.append(TechnicalDebtItem(
                            id=f"error_{uuid.uuid4().hex[:8]}",
                            category=TechnicalDebtCategory.ERROR_HANDLING,
                            file_path=str(file_path),
                            line_start=node.lineno,
                            line_end=node.lineno,
                            severity=0.6,
                            description="Silently catching and ignoring all exceptions",
                            code_snippet=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
                            suggested_fix="Log the exception or handle it appropriately",
                            estimated_effort="easy",
                        ))

        return items

    async def _check_type_safety(
        self,
        file_path: Path,
        tree: ast.AST,
        lines: List[str],
    ) -> List[TechnicalDebtItem]:
        """Check for type safety issues (missing type hints)."""
        items = []

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Skip private/magic methods and short functions
                if node.name.startswith('_') and not node.name.startswith('__'):
                    continue

                # Check for missing return type hint
                if node.returns is None and not node.name.startswith('__'):
                    # Check if function has return statements
                    has_return = any(
                        isinstance(n, ast.Return) and n.value is not None
                        for n in ast.walk(node)
                    )
                    if has_return:
                        items.append(TechnicalDebtItem(
                            id=f"type_{uuid.uuid4().hex[:8]}",
                            category=TechnicalDebtCategory.TYPE_SAFETY,
                            file_path=str(file_path),
                            line_start=node.lineno,
                            line_end=node.lineno,
                            severity=0.3,
                            description=f"Function '{node.name}' missing return type hint",
                            code_snippet=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
                            suggested_fix="Add return type annotation",
                            estimated_effort="trivial",
                        ))

                # Check for missing parameter type hints
                untyped_params = []
                for arg in node.args.args:
                    if arg.annotation is None and arg.arg != 'self' and arg.arg != 'cls':
                        untyped_params.append(arg.arg)

                if untyped_params and len(untyped_params) > 2:
                    items.append(TechnicalDebtItem(
                        id=f"type_{uuid.uuid4().hex[:8]}",
                        category=TechnicalDebtCategory.TYPE_SAFETY,
                        file_path=str(file_path),
                        line_start=node.lineno,
                        line_end=node.lineno,
                        severity=0.25,
                        description=f"Function '{node.name}' has {len(untyped_params)} untyped parameters: {', '.join(untyped_params[:3])}...",
                        code_snippet=lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
                        suggested_fix="Add type annotations to parameters",
                        estimated_effort="easy",
                    ))

        return items

    async def _add_oracle_detections(
        self,
        categories: Optional[List[TechnicalDebtCategory]],
    ) -> None:
        """Add detections from Oracle analysis."""
        if not self._oracle:
            return

        try:
            # Dead code detection
            if not categories or TechnicalDebtCategory.DEAD_CODE in categories:
                if hasattr(self._oracle, 'get_dead_code'):
                    dead_code = await asyncio.to_thread(self._oracle.get_dead_code)
                    for node in list(dead_code)[:20]:
                        self._scan_results.append(TechnicalDebtItem(
                            id=f"dead_{uuid.uuid4().hex[:8]}",
                            category=TechnicalDebtCategory.DEAD_CODE,
                            file_path=str(node) if hasattr(node, '__str__') else "unknown",
                            line_start=getattr(node, 'line_number', 0),
                            line_end=getattr(node, 'line_number', 0),
                            severity=0.4,
                            description=f"Dead code detected: {node}",
                            suggested_fix="Remove unused code or add usage",
                            estimated_effort="easy",
                        ))

            # Circular dependency detection
            if not categories or TechnicalDebtCategory.CIRCULAR_DEPS in categories:
                if hasattr(self._oracle, 'get_circular_dependencies'):
                    cycles = await asyncio.to_thread(self._oracle.get_circular_dependencies)
                    for cycle in list(cycles)[:10]:
                        self._scan_results.append(TechnicalDebtItem(
                            id=f"circ_{uuid.uuid4().hex[:8]}",
                            category=TechnicalDebtCategory.CIRCULAR_DEPS,
                            file_path=str(cycle[0]) if cycle else "unknown",
                            line_start=0,
                            line_end=0,
                            severity=0.75,
                            description=f"Circular dependency: {' -> '.join(str(c) for c in cycle)}",
                            suggested_fix="Refactor to remove circular import",
                            estimated_effort="hard",
                        ))

        except Exception as e:
            logger.debug(f"Oracle detection error: {e}")

    def _filter_results(
        self,
        categories: Optional[List[TechnicalDebtCategory]],
        max_issues: int,
    ) -> List[TechnicalDebtItem]:
        """Filter and limit results."""
        results = self._scan_results

        if categories:
            results = [r for r in results if r.category in categories]

        return results[:max_issues]

    def get_status(self) -> Dict[str, Any]:
        """Get detector status."""
        return {
            "total_issues": len(self._scan_results),
            "last_scan": self._last_scan,
            "oracle_connected": self._oracle is not None,
            "issues_by_category": {
                cat.value: len([r for r in self._scan_results if r.category == cat])
                for cat in TechnicalDebtCategory
            },
        }


class AutonomousSelfRefinementLoop:
    """
    v4.0: The "Think-Loop" - Continuous autonomous self-improvement.

    This is the EXECUTIVE CONTROL that makes JARVIS truly autonomous.
    It runs continuously in the background:
    1. Detects technical debt
    2. Decomposes issues into tasks
    3. Submits tasks to the orchestrator
    4. Learns from results

    The loop operates without human prompting - JARVIS decides what
    to improve and when.

    Safety features:
    - Configurable improvement budget (max tasks per hour)
    - Risk thresholds (won't touch critical systems without approval)
    - Learning from failures (avoids repeating mistakes)
    - Circuit breaker for consecutive failures
    - Priority-based scheduling
    """

    def __init__(
        self,
        debt_detector: Optional[TechnicalDebtDetector] = None,
        goal_decomposer: Optional[GoalDecompositionEngine] = None,
        orchestrator: Optional[Any] = None,  # AgenticLoopOrchestrator
        learning_db: Optional[Any] = None,
    ):
        """
        Initialize the autonomous refinement loop.

        Args:
            debt_detector: Technical debt detector instance
            goal_decomposer: Goal decomposition engine
            orchestrator: AgenticLoopOrchestrator for task execution
            learning_db: Learning database for pattern storage
        """
        self._debt_detector = debt_detector
        self._goal_decomposer = goal_decomposer
        self._orchestrator = orchestrator
        self._learning_db = learning_db

        self._running = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Configuration from environment
        self._scan_interval = int(os.getenv("AUTONOMOUS_SCAN_INTERVAL", "3600"))  # 1 hour
        self._max_tasks_per_hour = int(os.getenv("AUTONOMOUS_MAX_TASKS_HOUR", "5"))
        self._min_severity_threshold = float(os.getenv("AUTONOMOUS_MIN_SEVERITY", "0.5"))
        self._risk_threshold = float(os.getenv("AUTONOMOUS_RISK_THRESHOLD", "0.8"))
        self._consecutive_failure_limit = int(os.getenv("AUTONOMOUS_FAILURE_LIMIT", "3"))

        # State tracking
        self._tasks_this_hour: List[float] = []  # Timestamps of submitted tasks
        self._consecutive_failures = 0
        self._total_improvements = 0
        self._total_failures = 0
        self._avoided_issues: Set[str] = set()  # Issues we've learned to avoid

        # Metrics
        self._metrics = {
            "cycles_completed": 0,
            "issues_detected": 0,
            "tasks_submitted": 0,
            "improvements_successful": 0,
            "improvements_failed": 0,
            "issues_skipped_low_severity": 0,
            "issues_skipped_high_risk": 0,
            "issues_skipped_learned": 0,
        }

        logger.info("AutonomousSelfRefinementLoop initialized")

    async def start(self) -> None:
        """Start the autonomous refinement loop."""
        if self._running:
            logger.warning("AutonomousSelfRefinementLoop already running")
            return

        logger.info("Starting AutonomousSelfRefinementLoop...")
        self._running = True
        self._paused = False
        self._task = asyncio.create_task(self._refinement_loop())
        logger.info("AutonomousSelfRefinementLoop started")

    async def stop(self) -> None:
        """Stop the autonomous refinement loop."""
        if not self._running:
            return

        logger.info("Stopping AutonomousSelfRefinementLoop...")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        logger.info("AutonomousSelfRefinementLoop stopped")

    async def pause(self) -> None:
        """Pause the refinement loop (keeps running but skips processing)."""
        self._paused = True
        logger.info("AutonomousSelfRefinementLoop paused")

    async def resume(self) -> None:
        """Resume the refinement loop."""
        self._paused = False
        logger.info("AutonomousSelfRefinementLoop resumed")

    async def trigger_scan_now(self) -> List[TechnicalDebtItem]:
        """Trigger an immediate scan and return results."""
        if not self._debt_detector:
            return []

        return await self._debt_detector.scan_codebase(force_refresh=True)

    async def _refinement_loop(self) -> None:
        """Main refinement loop that runs continuously."""
        logger.info("Refinement loop started")

        while self._running:
            try:
                # Check if paused
                if self._paused:
                    await asyncio.sleep(10)
                    continue

                # Check circuit breaker
                if self._consecutive_failures >= self._consecutive_failure_limit:
                    logger.warning(
                        f"Circuit breaker open: {self._consecutive_failures} consecutive failures. "
                        f"Waiting 5 minutes before retry."
                    )
                    await asyncio.sleep(300)  # 5 minute cooldown
                    self._consecutive_failures = 0
                    continue

                # Run one refinement cycle
                await self._run_refinement_cycle()

                self._metrics["cycles_completed"] += 1

                # Wait before next cycle
                await asyncio.sleep(self._scan_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Refinement loop error: {e}")
                self._consecutive_failures += 1
                await asyncio.sleep(60)  # 1 minute cooldown on error

        logger.info("Refinement loop ended")

    async def _run_refinement_cycle(self) -> None:
        """Run a single refinement cycle."""
        logger.debug("Starting refinement cycle")

        # 1. Check task budget
        self._cleanup_old_tasks()
        remaining_budget = self._max_tasks_per_hour - len(self._tasks_this_hour)

        if remaining_budget <= 0:
            logger.debug("Task budget exhausted, skipping cycle")
            return

        # 2. Detect technical debt
        if not self._debt_detector:
            logger.warning("No debt detector configured")
            return

        issues = await self._debt_detector.scan_codebase()
        self._metrics["issues_detected"] += len(issues)

        if not issues:
            logger.debug("No technical debt detected")
            return

        # 3. Filter and prioritize issues
        actionable_issues = await self._filter_issues(issues)

        if not actionable_issues:
            logger.debug("No actionable issues after filtering")
            return

        logger.info(f"Found {len(actionable_issues)} actionable issues")

        # 4. Process top issues up to budget
        for issue in actionable_issues[:remaining_budget]:
            try:
                await self._process_issue(issue)
                self._consecutive_failures = 0  # Reset on success
            except Exception as e:
                logger.error(f"Failed to process issue {issue.id}: {e}")
                self._consecutive_failures += 1
                self._metrics["improvements_failed"] += 1

                # Learn from failure
                await self._record_failure(issue, str(e))

    async def _filter_issues(
        self,
        issues: List[TechnicalDebtItem],
    ) -> List[TechnicalDebtItem]:
        """Filter issues based on severity, risk, and learned patterns."""
        filtered = []

        for issue in issues:
            # Skip low severity
            if issue.severity < self._min_severity_threshold:
                self._metrics["issues_skipped_low_severity"] += 1
                continue

            # Skip high risk (security critical files, etc.)
            if await self._assess_risk(issue) > self._risk_threshold:
                self._metrics["issues_skipped_high_risk"] += 1
                continue

            # Skip issues we've learned to avoid
            issue_signature = self._get_issue_signature(issue)
            if issue_signature in self._avoided_issues:
                self._metrics["issues_skipped_learned"] += 1
                continue

            filtered.append(issue)

        # Sort by severity (highest first)
        filtered.sort(key=lambda x: x.severity, reverse=True)

        return filtered

    async def _assess_risk(self, issue: TechnicalDebtItem) -> float:
        """Assess the risk of fixing an issue."""
        risk = 0.0

        # Higher risk for certain files
        high_risk_patterns = [
            "security", "auth", "crypto", "payment", "billing",
            "database", "migration", "config", "secret",
        ]

        file_lower = issue.file_path.lower()
        for pattern in high_risk_patterns:
            if pattern in file_lower:
                risk += 0.2

        # Higher risk for high-severity security issues
        if issue.category == TechnicalDebtCategory.SECURITY:
            risk += 0.3

        # Use Oracle for blast radius if available
        if self._debt_detector and self._debt_detector._oracle:
            try:
                oracle = self._debt_detector._oracle
                if hasattr(oracle, 'get_blast_radius'):
                    blast = await asyncio.to_thread(
                        lambda: oracle.get_blast_radius(issue.file_path)
                    )
                    if blast and hasattr(blast, 'directly_affected'):
                        affected_count = len(blast.directly_affected)
                        risk += min(0.3, affected_count * 0.03)
            except Exception:
                pass

        return min(1.0, risk)

    def _get_issue_signature(self, issue: TechnicalDebtItem) -> str:
        """Get a unique signature for an issue (for learning)."""
        return f"{issue.category.value}:{issue.file_path}:{issue.line_start}"

    async def _process_issue(self, issue: TechnicalDebtItem) -> None:
        """Process a single issue by decomposing and submitting tasks."""
        logger.info(f"Processing issue: {issue.description[:50]}...")

        # Build improvement goal from issue
        goal = f"Fix {issue.category.value}: {issue.description}"

        # Decompose into sub-tasks
        if self._goal_decomposer:
            result = await self._goal_decomposer.decompose(
                goal=goal,
                context={
                    "issue_id": issue.id,
                    "category": issue.category.value,
                    "severity": issue.severity,
                    "file": issue.file_path,
                },
                target_files=[issue.file_path],
            )
            sub_tasks = result.sub_tasks
        else:
            # Fallback: single task
            sub_tasks = [SubTask(
                id=f"fix_{issue.id}",
                description=goal,
                task_type=SubTaskType.FIX,
                priority=SubTaskPriority.HIGH,
                target_files=[issue.file_path],
                parent_goal=goal,
            )]

        # Submit tasks to orchestrator
        if self._orchestrator:
            for sub_task in sub_tasks:
                await self._submit_task_to_orchestrator(sub_task, issue)
        else:
            logger.warning("No orchestrator configured, tasks not submitted")

        self._metrics["tasks_submitted"] += len(sub_tasks)
        self._tasks_this_hour.append(time.time())

    async def _submit_task_to_orchestrator(
        self,
        sub_task: SubTask,
        issue: TechnicalDebtItem,
    ) -> Optional[str]:
        """Submit a sub-task to the AgenticLoopOrchestrator."""
        if not self._orchestrator:
            return None

        try:
            # Map priority
            from enum import Enum

            # Import or create priority enum
            priority_value = 2  # Default to HIGH
            if sub_task.priority == SubTaskPriority.CRITICAL:
                priority_value = 1
            elif sub_task.priority == SubTaskPriority.MEDIUM:
                priority_value = 3
            elif sub_task.priority == SubTaskPriority.LOW:
                priority_value = 4

            # Find target file
            target_file = Path(issue.file_path)
            if not target_file.exists() and sub_task.target_files:
                target_file = Path(sub_task.target_files[0])

            if not target_file.exists():
                logger.warning(f"Target file not found: {target_file}")
                return None

            # Submit task
            if hasattr(self._orchestrator, 'submit_task'):
                task_id = await self._orchestrator.submit_task(
                    file_path=target_file,
                    goal=sub_task.description,
                    triggered_by="autonomous_refinement",
                )

                logger.info(f"Submitted task {task_id}: {sub_task.description[:50]}...")
                self._metrics["improvements_successful"] += 1
                return task_id

        except Exception as e:
            logger.error(f"Failed to submit task: {e}")
            return None

    async def _record_failure(self, issue: TechnicalDebtItem, error: str) -> None:
        """Record a failure for learning."""
        # Add to avoided issues if repeated failures
        issue_signature = self._get_issue_signature(issue)

        # Track in learning DB
        if self._learning_db and hasattr(self._learning_db, 'record_experience'):
            try:
                await self._learning_db.record_experience(
                    category="autonomous_refinement_failure",
                    data={
                        "issue_id": issue.id,
                        "category": issue.category.value,
                        "file": issue.file_path,
                        "error": error,
                        "signature": issue_signature,
                    }
                )
            except Exception:
                pass

        # After 2 failures on same issue, avoid it
        self._total_failures += 1
        if self._total_failures % 2 == 0:
            self._avoided_issues.add(issue_signature)

    def _cleanup_old_tasks(self) -> None:
        """Remove task timestamps older than 1 hour."""
        one_hour_ago = time.time() - 3600
        self._tasks_this_hour = [t for t in self._tasks_this_hour if t > one_hour_ago]

    def get_status(self) -> Dict[str, Any]:
        """Get loop status."""
        return {
            "running": self._running,
            "paused": self._paused,
            "consecutive_failures": self._consecutive_failures,
            "tasks_this_hour": len(self._tasks_this_hour),
            "max_tasks_per_hour": self._max_tasks_per_hour,
            "total_improvements": self._total_improvements,
            "avoided_issues_count": len(self._avoided_issues),
            "metrics": self._metrics.copy(),
        }


class DualAgentSystem:
    """
    v4.0: Architect/Reviewer dual-agent pattern for code improvements.

    This implements a two-phase approach:
    1. Architect agent generates the improvement
    2. Reviewer agent critiques and validates

    If the reviewer finds issues, the architect refines based on feedback.
    This catches errors before they're applied.

    Benefits:
    - Catches logical errors before application
    - Improves code quality through review
    - Reduces rollbacks from failed improvements
    - Enables different models for different roles
    """

    # Prompts for each role
    ARCHITECT_SYSTEM = """You are an expert software architect. Your job is to improve code based on goals while:
- Maintaining existing functionality
- Following best practices
- Writing clean, readable code
- Ensuring type safety
- Handling errors appropriately

Generate improved code that achieves the goal. Return the full improved code."""

    REVIEWER_SYSTEM = """You are a senior code reviewer. Analyze the proposed code changes and identify:
1. Logical errors or bugs
2. Security vulnerabilities
3. Performance issues
4. Code style violations
5. Missing error handling
6. Breaking changes

Be constructive and specific. If the code is good, say "APPROVED".
If issues found, list them clearly with specific line references.

Format:
VERDICT: APPROVED or NEEDS_REVISION
ISSUES: (if any)
- Issue 1 (line X): description
- Issue 2 (line Y): description
SUGGESTIONS: (optional improvements)"""

    def __init__(
        self,
        architect_client: Optional[Any] = None,
        reviewer_client: Optional[Any] = None,
        max_refinement_rounds: int = 3,
    ):
        """
        Initialize dual agent system.

        Args:
            architect_client: LLM client for architect agent
            reviewer_client: LLM client for reviewer agent (can be same)
            max_refinement_rounds: Maximum rounds of refinement
        """
        self._architect = architect_client
        self._reviewer = reviewer_client
        self._max_rounds = max_refinement_rounds
        self._lock = asyncio.Lock()

        # Metrics
        self._metrics = {
            "total_reviews": 0,
            "approved_first_try": 0,
            "approved_after_refinement": 0,
            "rejected": 0,
            "total_refinement_rounds": 0,
        }

        logger.info("DualAgentSystem initialized")

    async def improve_code(
        self,
        code: str,
        goal: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Improve code using architect/reviewer pattern.

        Args:
            code: Original code to improve
            goal: Improvement goal
            context: Additional context

        Returns:
            Tuple of (improved_code, metadata)
        """
        async with self._lock:
            self._metrics["total_reviews"] += 1

            current_code = code
            refinement_round = 0
            review_history = []

            while refinement_round < self._max_rounds:
                # 1. Architect generates improvement
                improved = await self._architect_generate(current_code, goal, context, review_history)

                if not improved or improved == current_code:
                    logger.warning("Architect produced no changes")
                    break

                # 2. Reviewer evaluates
                review = await self._reviewer_review(code, improved, goal)
                review_history.append(review)

                # 3. Check verdict
                if review.get("approved", False):
                    if refinement_round == 0:
                        self._metrics["approved_first_try"] += 1
                    else:
                        self._metrics["approved_after_refinement"] += 1

                    return improved, {
                        "approved": True,
                        "rounds": refinement_round + 1,
                        "review_history": review_history,
                    }

                # 4. Refinement needed
                refinement_round += 1
                self._metrics["total_refinement_rounds"] += 1
                current_code = improved

                logger.debug(f"Refinement round {refinement_round}: {review.get('issues', [])}")

            # Max rounds reached or no improvement
            self._metrics["rejected"] += 1
            return code, {  # Return original code
                "approved": False,
                "rounds": refinement_round,
                "review_history": review_history,
                "reason": "Max refinement rounds reached or no improvement",
            }

    async def _architect_generate(
        self,
        code: str,
        goal: str,
        context: Optional[Dict[str, Any]],
        review_history: List[Dict[str, Any]],
    ) -> str:
        """Generate improved code using architect agent."""
        if not self._architect:
            return code

        # Build prompt
        prompt_parts = [
            f"# Goal\n{goal}\n",
            f"# Original Code\n```python\n{code}\n```\n",
        ]

        if context:
            prompt_parts.append(f"# Context\n{json.dumps(context, indent=2)}\n")

        if review_history:
            prompt_parts.append("# Previous Review Feedback")
            for i, review in enumerate(review_history):
                if review.get("issues"):
                    prompt_parts.append(f"Round {i+1} issues: {review['issues']}")
            prompt_parts.append("")

        prompt_parts.append(
            "Generate improved code that achieves the goal and addresses any previous feedback. "
            "Return ONLY the improved code, no explanation."
        )

        prompt = "\n".join(prompt_parts)

        try:
            response = await self._call_llm(self._architect, self.ARCHITECT_SYSTEM, prompt)

            # Extract code from response
            code_match = re.search(r'```python\n(.*?)```', response, re.DOTALL)
            if code_match:
                return code_match.group(1).strip()
            return response.strip()

        except Exception as e:
            logger.error(f"Architect generation failed: {e}")
            return code

    async def _reviewer_review(
        self,
        original: str,
        improved: str,
        goal: str,
    ) -> Dict[str, Any]:
        """Review improved code using reviewer agent."""
        if not self._reviewer:
            return {"approved": True, "issues": []}

        prompt = f"""# Goal
{goal}

# Original Code
```python
{original}
```

# Proposed Improvement
```python
{improved}
```

Review the proposed changes. Is this improvement correct and safe to apply?"""

        try:
            response = await self._call_llm(self._reviewer, self.REVIEWER_SYSTEM, prompt)

            # Parse response
            approved = "APPROVED" in response.upper() and "NEEDS_REVISION" not in response.upper()

            # Extract issues
            issues = []
            if "ISSUES:" in response.upper():
                issues_section = response.split("ISSUES:")[1].split("SUGGESTIONS:")[0] if "SUGGESTIONS:" in response else response.split("ISSUES:")[1]
                issues = [line.strip() for line in issues_section.strip().split("\n") if line.strip().startswith("-")]

            return {
                "approved": approved,
                "issues": issues,
                "raw_response": response,
            }

        except Exception as e:
            logger.error(f"Reviewer evaluation failed: {e}")
            return {"approved": True, "issues": [], "error": str(e)}

    async def _call_llm(self, client: Any, system: str, prompt: str) -> str:
        """Call LLM with various client interfaces."""
        try:
            if hasattr(client, 'complete'):
                response = await client.complete(
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=4000,
                    temperature=0.3,
                )
                return response[0] if isinstance(response, tuple) else response
            elif hasattr(client, 'generate'):
                return await client.generate(
                    prompt=f"{system}\n\n{prompt}",
                    max_tokens=4000,
                )
            elif hasattr(client, 'chat'):
                response = await client.chat(
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=4000,
                )
                return response.get("content", "") if isinstance(response, dict) else str(response)
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return ""

        return ""

    def get_status(self) -> Dict[str, Any]:
        """Get system status."""
        return {
            "architect_connected": self._architect is not None,
            "reviewer_connected": self._reviewer is not None,
            "max_rounds": self._max_rounds,
            "metrics": self._metrics.copy(),
        }


class CodeMemoryRAG:
    """
    v4.0: Unified Oracle + ChromaDB code memory with GraphRAG.

    This fuses two memory systems:
    - Oracle (graph): Structural relationships (imports, calls, inheritance)
    - ChromaDB (semantic): Conceptual similarity and semantic search

    The combination enables:
    - "Find code similar to authentication" (semantic)
    - "Find code that calls the auth module" (graph)
    - "Find similar code that's structurally related" (hybrid)

    This is critical for intelligent code navigation and improvement.
    """

    def __init__(
        self,
        oracle: Optional[Any] = None,
        chromadb_client: Optional[Any] = None,
        embedding_model: Optional[Any] = None,
    ):
        """
        Initialize unified code memory.

        Args:
            oracle: The Oracle codebase knowledge graph
            chromadb_client: ChromaDB client for semantic memory
            embedding_model: Model for generating embeddings
        """
        self._oracle = oracle
        self._chromadb = chromadb_client
        self._embedding_model = embedding_model
        self._lock = asyncio.Lock()
        self._collection_name = os.getenv("CODE_MEMORY_COLLECTION", "jarvis_code_memory")

        logger.info("CodeMemoryRAG initialized")

    async def remember_code(
        self,
        file_path: str,
        code: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Index code in both Oracle (structure) and ChromaDB (semantics).

        Args:
            file_path: Path to the file
            code: Code content
            metadata: Additional metadata
        """
        async with self._lock:
            # Index in Oracle (structural)
            if self._oracle:
                try:
                    if hasattr(self._oracle, 'index_file'):
                        await asyncio.to_thread(
                            lambda: self._oracle.index_file(Path(file_path))
                        )
                except Exception as e:
                    logger.debug(f"Oracle indexing error: {e}")

            # Index in ChromaDB (semantic)
            if self._chromadb:
                try:
                    await self._index_in_chromadb(file_path, code, metadata)
                except Exception as e:
                    logger.debug(f"ChromaDB indexing error: {e}")

    async def _index_in_chromadb(
        self,
        file_path: str,
        code: str,
        metadata: Optional[Dict[str, Any]],
    ) -> None:
        """Index code in ChromaDB."""
        # Get or create collection
        if hasattr(self._chromadb, 'get_or_create_collection'):
            collection = self._chromadb.get_or_create_collection(
                name=self._collection_name,
            )
        else:
            return

        # Split code into chunks
        chunks = self._chunk_code(code)

        # Generate embeddings
        embeddings = []
        if self._embedding_model:
            for chunk in chunks:
                try:
                    if hasattr(self._embedding_model, 'encode'):
                        emb = self._embedding_model.encode(chunk)
                        embeddings.append(emb.tolist() if hasattr(emb, 'tolist') else emb)
                except Exception:
                    pass

        # Store in ChromaDB
        for i, chunk in enumerate(chunks):
            doc_id = f"{hashlib.sha256(file_path.encode()).hexdigest()[:12]}_{i}"
            chunk_metadata = {
                "file_path": file_path,
                "chunk_index": i,
                "total_chunks": len(chunks),
                **(metadata or {}),
            }

            try:
                if embeddings and i < len(embeddings):
                    collection.upsert(
                        ids=[doc_id],
                        documents=[chunk],
                        metadatas=[chunk_metadata],
                        embeddings=[embeddings[i]],
                    )
                else:
                    collection.upsert(
                        ids=[doc_id],
                        documents=[chunk],
                        metadatas=[chunk_metadata],
                    )
            except Exception as e:
                logger.debug(f"ChromaDB upsert error: {e}")

    def _chunk_code(self, code: str, chunk_size: int = 500) -> List[str]:
        """Split code into semantic chunks (at function/class boundaries)."""
        chunks = []

        try:
            tree = ast.parse(code)
            lines = code.splitlines()

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = node.lineno - 1
                    end = getattr(node, 'end_lineno', start + 10)
                    chunk = "\n".join(lines[start:end])
                    if chunk.strip():
                        chunks.append(chunk)

            # Add module-level code
            if not chunks:
                # No functions/classes, chunk by lines
                for i in range(0, len(lines), chunk_size // 50):
                    chunk = "\n".join(lines[i:i + chunk_size // 50])
                    if chunk.strip():
                        chunks.append(chunk)

        except SyntaxError:
            # Fallback to simple line-based chunking
            lines = code.splitlines()
            for i in range(0, len(lines), chunk_size // 50):
                chunk = "\n".join(lines[i:i + chunk_size // 50])
                if chunk.strip():
                    chunks.append(chunk)

        return chunks or [code]

    async def find_related_code(
        self,
        query: str,
        limit: int = 10,
        use_graph: bool = True,
        use_semantic: bool = True,
    ) -> CodeMemoryResult:
        """
        Find code related to a query using hybrid search.

        Args:
            query: Search query (natural language or code)
            limit: Maximum results to return
            use_graph: Include Oracle graph search
            use_semantic: Include ChromaDB semantic search

        Returns:
            CodeMemoryResult with merged results
        """
        graph_results = []
        semantic_results = []

        # Graph search (Oracle)
        if use_graph and self._oracle:
            graph_results = await self._search_oracle(query, limit)

        # Semantic search (ChromaDB)
        if use_semantic and self._chromadb:
            semantic_results = await self._search_chromadb(query, limit)

        # Merge results
        merged, scores = self._merge_results(graph_results, semantic_results)

        return CodeMemoryResult(
            query=query,
            graph_results=graph_results,
            semantic_results=semantic_results,
            merged_results=merged[:limit],
            relevance_scores=scores,
            total_results=len(merged),
        )

    async def _search_oracle(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Search Oracle graph database."""
        results = []

        try:
            # Extract keywords
            keywords = re.findall(r'\b[a-zA-Z_][a-zA-Z0-9_]*\b', query)

            for keyword in keywords[:5]:
                if hasattr(self._oracle, 'find_nodes_by_name'):
                    nodes = await asyncio.to_thread(
                        lambda k=keyword: self._oracle.find_nodes_by_name(k, fuzzy=True)
                    )
                    for node in nodes[:limit // 2]:
                        results.append({
                            "source": "oracle",
                            "type": "graph",
                            "node": str(node),
                            "keyword": keyword,
                        })
        except Exception as e:
            logger.debug(f"Oracle search error: {e}")

        return results

    async def _search_chromadb(self, query: str, limit: int) -> List[Dict[str, Any]]:
        """Search ChromaDB semantic memory."""
        results = []

        try:
            if hasattr(self._chromadb, 'get_or_create_collection'):
                collection = self._chromadb.get_or_create_collection(
                    name=self._collection_name
                )

                # Generate query embedding
                query_embedding = None
                if self._embedding_model and hasattr(self._embedding_model, 'encode'):
                    emb = self._embedding_model.encode(query)
                    query_embedding = emb.tolist() if hasattr(emb, 'tolist') else emb

                # Search
                if query_embedding:
                    search_results = collection.query(
                        query_embeddings=[query_embedding],
                        n_results=limit,
                    )
                else:
                    search_results = collection.query(
                        query_texts=[query],
                        n_results=limit,
                    )

                # Format results
                if search_results and search_results.get("documents"):
                    for i, doc in enumerate(search_results["documents"][0]):
                        metadata = search_results.get("metadatas", [[]])[0]
                        meta = metadata[i] if i < len(metadata) else {}
                        results.append({
                            "source": "chromadb",
                            "type": "semantic",
                            "content": doc[:200] + "..." if len(doc) > 200 else doc,
                            "file_path": meta.get("file_path", "unknown"),
                            "score": search_results.get("distances", [[]])[0][i]
                            if search_results.get("distances") else 0,
                        })

        except Exception as e:
            logger.debug(f"ChromaDB search error: {e}")

        return results

    def _merge_results(
        self,
        graph_results: List[Dict[str, Any]],
        semantic_results: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """Merge and rank results from both sources."""
        merged = []
        scores = {}

        # Score and deduplicate
        seen_files = set()

        # Add semantic results (higher priority for similarity)
        for result in semantic_results:
            file_path = result.get("file_path", "")
            if file_path and file_path not in seen_files:
                seen_files.add(file_path)
                score = 1.0 - result.get("score", 0.5)  # ChromaDB returns distance
                scores[file_path] = score
                result["combined_score"] = score
                merged.append(result)

        # Add graph results
        for result in graph_results:
            node = result.get("node", "")
            if node not in seen_files:
                seen_files.add(node)
                score = 0.7  # Base score for graph matches
                scores[node] = score
                result["combined_score"] = score
                merged.append(result)

        # Sort by score
        merged.sort(key=lambda x: x.get("combined_score", 0), reverse=True)

        return merged, scores

    def get_status(self) -> Dict[str, Any]:
        """Get memory status."""
        return {
            "oracle_connected": self._oracle is not None,
            "chromadb_connected": self._chromadb is not None,
            "embedding_model_connected": self._embedding_model is not None,
            "collection_name": self._collection_name,
        }


class SystemFeedbackLoop:
    """
    v4.0: Hardware metrics → code improvements feedback loop.

    Monitors system performance and triggers code improvements
    when resource usage exceeds thresholds.

    Example:
    - Memory > 85% → Trigger memory optimization task
    - CPU > 90% → Trigger performance optimization
    - Slow API responses → Profile and optimize
    """

    def __init__(
        self,
        orchestrator: Optional[Any] = None,
        goal_decomposer: Optional[GoalDecompositionEngine] = None,
    ):
        """
        Initialize system feedback loop.

        Args:
            orchestrator: AgenticLoopOrchestrator for task submission
            goal_decomposer: Goal decomposition engine
        """
        self._orchestrator = orchestrator
        self._goal_decomposer = goal_decomposer
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

        # Thresholds (from environment)
        self._memory_threshold = float(os.getenv("FEEDBACK_MEMORY_THRESHOLD", "0.85"))
        self._cpu_threshold = float(os.getenv("FEEDBACK_CPU_THRESHOLD", "0.90"))
        self._disk_threshold = float(os.getenv("FEEDBACK_DISK_THRESHOLD", "0.90"))
        self._check_interval = int(os.getenv("FEEDBACK_CHECK_INTERVAL", "60"))

        # Cooldown to prevent spam
        self._last_memory_alert: Optional[float] = None
        self._last_cpu_alert: Optional[float] = None
        self._alert_cooldown = 3600  # 1 hour

        # Metrics
        self._metrics = {
            "checks_performed": 0,
            "memory_alerts": 0,
            "cpu_alerts": 0,
            "tasks_triggered": 0,
        }

        logger.info("SystemFeedbackLoop initialized")

    async def start(self) -> None:
        """Start the feedback monitoring loop."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("SystemFeedbackLoop started")

    async def stop(self) -> None:
        """Stop the feedback monitoring loop."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        logger.info("SystemFeedbackLoop stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self._running:
            try:
                metrics = await self._get_system_metrics()
                self._metrics["checks_performed"] += 1

                # Check memory
                if metrics.get("memory_percent", 0) > self._memory_threshold * 100:
                    await self._handle_high_memory(metrics)

                # Check CPU
                if metrics.get("cpu_percent", 0) > self._cpu_threshold * 100:
                    await self._handle_high_cpu(metrics)

                await asyncio.sleep(self._check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                await asyncio.sleep(60)

    async def _get_system_metrics(self) -> Dict[str, Any]:
        """Get current system metrics."""
        metrics = {}

        try:
            import psutil

            # Memory
            memory = psutil.virtual_memory()
            metrics["memory_percent"] = memory.percent
            metrics["memory_available_gb"] = memory.available / (1024**3)

            # CPU
            metrics["cpu_percent"] = psutil.cpu_percent(interval=1)

            # Disk
            disk = psutil.disk_usage('/')
            metrics["disk_percent"] = disk.percent

        except ImportError:
            # Fallback without psutil
            metrics["memory_percent"] = 0
            metrics["cpu_percent"] = 0
            metrics["disk_percent"] = 0
        except Exception as e:
            logger.debug(f"Metrics collection error: {e}")

        return metrics

    async def _handle_high_memory(self, metrics: Dict[str, Any]) -> None:
        """Handle high memory usage."""
        now = time.time()

        # Check cooldown
        if self._last_memory_alert and now - self._last_memory_alert < self._alert_cooldown:
            return

        self._last_memory_alert = now
        self._metrics["memory_alerts"] += 1

        logger.warning(f"High memory usage: {metrics.get('memory_percent', 0):.1f}%")

        # Submit optimization task
        if self._orchestrator:
            goal = f"Optimize memory usage - currently at {metrics.get('memory_percent', 0):.1f}%"

            if self._goal_decomposer:
                result = await self._goal_decomposer.decompose(goal)
                for task in result.sub_tasks:
                    if task.target_files:
                        await self._submit_task(task.target_files[0], task.description)
            else:
                await self._submit_task(
                    "backend/core/ouroboros/integration.py",
                    goal,
                )

    async def _handle_high_cpu(self, metrics: Dict[str, Any]) -> None:
        """Handle high CPU usage."""
        now = time.time()

        # Check cooldown
        if self._last_cpu_alert and now - self._last_cpu_alert < self._alert_cooldown:
            return

        self._last_cpu_alert = now
        self._metrics["cpu_alerts"] += 1

        logger.warning(f"High CPU usage: {metrics.get('cpu_percent', 0):.1f}%")

        # Submit optimization task
        if self._orchestrator:
            goal = f"Optimize CPU usage - currently at {metrics.get('cpu_percent', 0):.1f}%"
            await self._submit_task(
                "backend/core/ouroboros/integration.py",
                goal,
            )

    async def _submit_task(self, file_path: str, goal: str) -> None:
        """Submit optimization task to orchestrator."""
        if not self._orchestrator or not hasattr(self._orchestrator, 'submit_task'):
            return

        try:
            await self._orchestrator.submit_task(
                file_path=Path(file_path),
                goal=goal,
                triggered_by="system_feedback",
            )
            self._metrics["tasks_triggered"] += 1
        except Exception as e:
            logger.error(f"Failed to submit task: {e}")

    def get_status(self) -> Dict[str, Any]:
        """Get loop status."""
        return {
            "running": self._running,
            "orchestrator_connected": self._orchestrator is not None,
            "thresholds": {
                "memory": self._memory_threshold,
                "cpu": self._cpu_threshold,
                "disk": self._disk_threshold,
            },
            "metrics": self._metrics.copy(),
        }


class AutoTestGenerator:
    """
    v4.0: Automatic test generation for existing untested code.

    Scans the codebase for functions/classes without tests and
    generates pytest tests for them.

    Features:
    - Identifies untested code through coverage analysis
    - Generates meaningful test cases using LLM
    - Validates tests actually pass
    - Integrates with existing test suites
    """

    def __init__(
        self,
        project_root: Optional[Path] = None,
        llm_client: Optional[Any] = None,
    ):
        """
        Initialize auto test generator.

        Args:
            project_root: Root directory of the project
            llm_client: LLM client for test generation
        """
        self._project_root = project_root or Path(os.getenv(
            "JARVIS_PATH",
            Path.home() / "Documents/repos/JARVIS-AI-Agent"
        ))
        self._llm_client = llm_client
        self._lock = asyncio.Lock()

        logger.info("AutoTestGenerator initialized")

    async def find_untested_code(
        self,
        target_dir: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Find functions and classes without test coverage.

        Args:
            target_dir: Directory to scan (default: backend/)

        Returns:
            List of untested items with metadata
        """
        scan_dir = self._project_root / (target_dir or "backend")
        test_dir = self._project_root / "tests"

        untested = []

        # Get all Python files
        source_files = list(scan_dir.rglob("*.py"))
        source_files = [f for f in source_files if "__pycache__" not in str(f)]

        # Get all test files
        test_files = list(test_dir.rglob("test_*.py")) if test_dir.exists() else []

        # Build set of tested items
        tested_items = set()
        for test_file in test_files:
            try:
                content = await asyncio.to_thread(test_file.read_text)
                # Extract tested function/class names
                matches = re.findall(r'def test_(\w+)', content)
                matches += re.findall(r'class Test(\w+)', content)
                tested_items.update(m.lower() for m in matches)
            except Exception:
                pass

        # Find untested items
        for source_file in source_files[:50]:  # Limit to avoid overwhelming
            try:
                content = await asyncio.to_thread(source_file.read_text)
                tree = ast.parse(content)

                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Skip private and magic methods
                        if node.name.startswith('_'):
                            continue

                        # Check if tested
                        if node.name.lower() not in tested_items:
                            untested.append({
                                "type": "function",
                                "name": node.name,
                                "file": str(source_file),
                                "line": node.lineno,
                                "signature": self._get_signature(node),
                                "docstring": ast.get_docstring(node),
                            })

                    elif isinstance(node, ast.ClassDef):
                        if node.name.startswith('_'):
                            continue

                        if node.name.lower() not in tested_items:
                            untested.append({
                                "type": "class",
                                "name": node.name,
                                "file": str(source_file),
                                "line": node.lineno,
                                "docstring": ast.get_docstring(node),
                            })
            except Exception:
                pass

        return untested

    def _get_signature(self, node: ast.FunctionDef) -> str:
        """Extract function signature."""
        args = []
        for arg in node.args.args:
            arg_str = arg.arg
            if arg.annotation:
                try:
                    arg_str += f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            args.append(arg_str)

        return_hint = ""
        if node.returns:
            try:
                return_hint = f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass

        return f"def {node.name}({', '.join(args)}){return_hint}"

    async def generate_tests(
        self,
        item: Dict[str, Any],
        dry_run: bool = True,
    ) -> str:
        """
        Generate tests for an untested item.

        Args:
            item: Untested item metadata
            dry_run: If True, only return tests without saving

        Returns:
            Generated test code
        """
        if not self._llm_client:
            return self._generate_basic_test(item)

        # Read source code
        try:
            source_file = Path(item["file"])
            content = await asyncio.to_thread(source_file.read_text)
        except Exception:
            content = ""

        # Build prompt
        prompt = f"""Generate pytest tests for the following {item['type']}:

Name: {item['name']}
Signature: {item.get('signature', 'N/A')}
Docstring: {item.get('docstring', 'No documentation')}

Source file: {item['file']}

Source code context:
```python
{content[:3000]}
```

Generate comprehensive pytest tests including:
1. Happy path test cases
2. Edge cases
3. Error handling tests (if applicable)

Use pytest fixtures where appropriate.
Include docstrings for each test.
Return only the test code, no explanation."""

        try:
            system = "You are a test engineering expert. Generate comprehensive, clean pytest tests."

            if hasattr(self._llm_client, 'complete'):
                response = await self._llm_client.complete(
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=2000,
                )
                test_code = response[0] if isinstance(response, tuple) else response
            else:
                test_code = self._generate_basic_test(item)

            # Extract code from response
            code_match = re.search(r'```python\n(.*?)```', test_code, re.DOTALL)
            if code_match:
                test_code = code_match.group(1)

            # Save if not dry run
            if not dry_run:
                await self._save_tests(item, test_code)

            return test_code

        except Exception as e:
            logger.error(f"Test generation failed: {e}")
            return self._generate_basic_test(item)

    def _generate_basic_test(self, item: Dict[str, Any]) -> str:
        """Generate basic test template without LLM."""
        name = item["name"]
        item_type = item["type"]

        if item_type == "function":
            return f'''import pytest
from {self._get_import_path(item["file"])} import {name}


def test_{name}_basic():
    """Basic test for {name}."""
    # TODO: Implement test
    pass


def test_{name}_edge_case():
    """Edge case test for {name}."""
    # TODO: Implement test
    pass
'''
        else:  # class
            return f'''import pytest
from {self._get_import_path(item["file"])} import {name}


class Test{name}:
    """Tests for {name} class."""

    def test_init(self):
        """Test {name} initialization."""
        # TODO: Implement test
        pass

    def test_basic_usage(self):
        """Test basic {name} usage."""
        # TODO: Implement test
        pass
'''

    def _get_import_path(self, file_path: str) -> str:
        """Convert file path to import path."""
        rel_path = Path(file_path).relative_to(self._project_root)
        import_path = str(rel_path).replace("/", ".").replace("\\", ".").replace(".py", "")
        return import_path

    async def _save_tests(self, item: Dict[str, Any], test_code: str) -> None:
        """Save generated tests to file."""
        source_file = Path(item["file"])
        rel_path = source_file.relative_to(self._project_root)

        # Create test file path
        test_path = self._project_root / "tests" / f"test_{rel_path.stem}.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)

        # Append or create
        if test_path.exists():
            existing = await asyncio.to_thread(test_path.read_text)
            if item["name"] not in existing:
                test_code = f"\n\n{test_code}"
                await asyncio.to_thread(
                    lambda: test_path.write_text(existing + test_code)
                )
        else:
            await asyncio.to_thread(
                lambda: test_path.write_text(test_code)
            )

        logger.info(f"Saved tests to {test_path}")

    def get_status(self) -> Dict[str, Any]:
        """Get generator status."""
        return {
            "project_root": str(self._project_root),
            "llm_connected": self._llm_client is not None,
        }


# =============================================================================
# v4.0: GLOBAL INSTANCES AND FACTORY FUNCTIONS
# =============================================================================

_goal_decomposer: Optional[GoalDecompositionEngine] = None
_debt_detector: Optional[TechnicalDebtDetector] = None
_refinement_loop: Optional[AutonomousSelfRefinementLoop] = None
_dual_agent_system: Optional[DualAgentSystem] = None
_code_memory_rag: Optional[CodeMemoryRAG] = None
_system_feedback_loop: Optional[SystemFeedbackLoop] = None
_auto_test_generator: Optional[AutoTestGenerator] = None


def get_goal_decomposer(
    oracle: Optional[Any] = None,
    llm_client: Optional[Any] = None,
) -> GoalDecompositionEngine:
    """Get or create the goal decomposition engine."""
    global _goal_decomposer
    if _goal_decomposer is None:
        _goal_decomposer = GoalDecompositionEngine(
            oracle=oracle,
            llm_client=llm_client,
        )
    return _goal_decomposer


def get_debt_detector(
    oracle: Optional[Any] = None,
    project_root: Optional[Path] = None,
) -> TechnicalDebtDetector:
    """Get or create the technical debt detector."""
    global _debt_detector
    if _debt_detector is None:
        _debt_detector = TechnicalDebtDetector(
            oracle=oracle,
            project_root=project_root,
        )
    return _debt_detector


def get_refinement_loop(
    debt_detector: Optional[TechnicalDebtDetector] = None,
    goal_decomposer: Optional[GoalDecompositionEngine] = None,
    orchestrator: Optional[Any] = None,
) -> AutonomousSelfRefinementLoop:
    """Get or create the autonomous refinement loop."""
    global _refinement_loop
    if _refinement_loop is None:
        _refinement_loop = AutonomousSelfRefinementLoop(
            debt_detector=debt_detector,
            goal_decomposer=goal_decomposer,
            orchestrator=orchestrator,
        )
    return _refinement_loop


def get_dual_agent_system(
    architect_client: Optional[Any] = None,
    reviewer_client: Optional[Any] = None,
) -> DualAgentSystem:
    """Get or create the dual agent system."""
    global _dual_agent_system
    if _dual_agent_system is None:
        _dual_agent_system = DualAgentSystem(
            architect_client=architect_client,
            reviewer_client=reviewer_client,
        )
    return _dual_agent_system


def get_code_memory_rag(
    oracle: Optional[Any] = None,
    chromadb_client: Optional[Any] = None,
) -> CodeMemoryRAG:
    """Get or create the code memory RAG."""
    global _code_memory_rag
    if _code_memory_rag is None:
        _code_memory_rag = CodeMemoryRAG(
            oracle=oracle,
            chromadb_client=chromadb_client,
        )
    return _code_memory_rag


def get_system_feedback_loop(
    orchestrator: Optional[Any] = None,
    goal_decomposer: Optional[GoalDecompositionEngine] = None,
) -> SystemFeedbackLoop:
    """Get or create the system feedback loop."""
    global _system_feedback_loop
    if _system_feedback_loop is None:
        _system_feedback_loop = SystemFeedbackLoop(
            orchestrator=orchestrator,
            goal_decomposer=goal_decomposer,
        )
    return _system_feedback_loop


def get_auto_test_generator(
    project_root: Optional[Path] = None,
    llm_client: Optional[Any] = None,
) -> AutoTestGenerator:
    """Get or create the auto test generator."""
    global _auto_test_generator
    if _auto_test_generator is None:
        _auto_test_generator = AutoTestGenerator(
            project_root=project_root,
            llm_client=llm_client,
        )
    return _auto_test_generator


async def initialize_autonomous_self_programming(
    oracle: Optional[Any] = None,
    orchestrator: Optional[Any] = None,
    llm_client: Optional[Any] = None,
    chromadb_client: Optional[Any] = None,
    start_loops: bool = False,
) -> Dict[str, Any]:
    """
    Initialize all autonomous self-programming components.

    This is the master initialization function that sets up:
    - GoalDecompositionEngine
    - TechnicalDebtDetector
    - AutonomousSelfRefinementLoop
    - DualAgentSystem
    - CodeMemoryRAG
    - SystemFeedbackLoop
    - AutoTestGenerator

    Args:
        oracle: The Oracle codebase knowledge graph
        orchestrator: AgenticLoopOrchestrator for task execution
        llm_client: LLM client for goal decomposition and test generation
        chromadb_client: ChromaDB client for semantic memory
        start_loops: Whether to start background loops

    Returns:
        Dictionary with all initialized components
    """
    logger.info("Initializing autonomous self-programming components...")

    # Initialize components
    goal_decomposer = get_goal_decomposer(oracle=oracle, llm_client=llm_client)
    debt_detector = get_debt_detector(oracle=oracle)
    dual_agent = get_dual_agent_system(architect_client=llm_client, reviewer_client=llm_client)
    code_memory = get_code_memory_rag(oracle=oracle, chromadb_client=chromadb_client)

    # Initialize loops (but don't start yet)
    refinement_loop = get_refinement_loop(
        debt_detector=debt_detector,
        goal_decomposer=goal_decomposer,
        orchestrator=orchestrator,
    )

    system_feedback = get_system_feedback_loop(
        orchestrator=orchestrator,
        goal_decomposer=goal_decomposer,
    )

    test_generator = get_auto_test_generator(llm_client=llm_client)

    # Start background loops if requested
    if start_loops:
        await refinement_loop.start()
        await system_feedback.start()

    components = {
        "goal_decomposer": goal_decomposer,
        "debt_detector": debt_detector,
        "refinement_loop": refinement_loop,
        "dual_agent_system": dual_agent,
        "code_memory_rag": code_memory,
        "system_feedback_loop": system_feedback,
        "auto_test_generator": test_generator,
    }

    logger.info(f"Autonomous self-programming initialized with {len(components)} components")
    return components


async def shutdown_autonomous_self_programming() -> None:
    """Shutdown all autonomous self-programming components."""
    logger.info("Shutting down autonomous self-programming components...")

    global _refinement_loop, _system_feedback_loop

    if _refinement_loop:
        await _refinement_loop.stop()

    if _system_feedback_loop:
        await _system_feedback_loop.stop()

    logger.info("Autonomous self-programming shutdown complete")


# =============================================================================
# v5.0: Web Search Integration for Autonomous Self-Programming
# =============================================================================
# This module provides web search capabilities for the self-programming system:
# - WebSearchExtractor: Multi-provider web search with fallback
# - DocumentationLookup: Official library documentation retrieval
# - StackOverflowSearcher: Error solutions from Stack Overflow
# - CodeExampleScraper: Code examples from GitHub
# - OuroborosWebIntegration: Integration with the improvement loop
# =============================================================================


class SearchProvider(Enum):
    """Supported web search providers."""
    DUCKDUCKGO = "duckduckgo"
    BRAVE = "brave"
    BING = "bing"
    GOOGLE = "google"
    SEARXNG = "searxng"


class SearchResultType(Enum):
    """Types of search results."""
    WEB_PAGE = "web_page"
    DOCUMENTATION = "documentation"
    STACK_OVERFLOW = "stack_overflow"
    GITHUB_CODE = "github_code"
    GITHUB_REPO = "github_repo"
    API_REFERENCE = "api_reference"
    TUTORIAL = "tutorial"
    BLOG_POST = "blog_post"


class DocumentationType(Enum):
    """Types of documentation sources."""
    OFFICIAL = "official"
    COMMUNITY = "community"
    API_REFERENCE = "api_reference"
    TUTORIAL = "tutorial"
    CHANGELOG = "changelog"
    README = "readme"


@dataclass
class SearchResult:
    """A single search result."""
    title: str
    url: str
    snippet: str
    provider: SearchProvider
    result_type: SearchResultType
    relevance_score: float  # 0.0 to 1.0
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    cache_expires: Optional[datetime] = None


@dataclass
class DocumentationResult:
    """Documentation lookup result."""
    library_name: str
    version: Optional[str]
    doc_type: DocumentationType
    title: str
    url: str
    content: str
    code_examples: List[str] = field(default_factory=list)
    related_topics: List[str] = field(default_factory=list)
    last_updated: Optional[datetime] = None
    relevance_score: float = 0.0


@dataclass
class StackOverflowResult:
    """Stack Overflow search result."""
    question_id: int
    question_title: str
    question_url: str
    question_score: int
    answer_count: int
    is_answered: bool
    accepted_answer_id: Optional[int]
    tags: List[str]
    question_body: str
    answers: List[Dict[str, Any]] = field(default_factory=list)
    relevance_score: float = 0.0


@dataclass
class CodeExample:
    """Code example from GitHub or other sources."""
    source: str  # 'github', 'gist', 'docs', etc.
    repo_name: Optional[str]
    file_path: Optional[str]
    code: str
    language: str
    context: str  # Surrounding description
    stars: int = 0
    url: str = ""
    relevance_score: float = 0.0


@dataclass
class WebSearchConfig:
    """Configuration for web search components."""
    # Provider configuration
    primary_provider: SearchProvider = SearchProvider.DUCKDUCKGO
    fallback_providers: List[SearchProvider] = field(
        default_factory=lambda: [SearchProvider.BRAVE, SearchProvider.BING]
    )

    # Rate limiting (requests per minute per provider)
    rate_limits: Dict[SearchProvider, int] = field(default_factory=lambda: {
        SearchProvider.DUCKDUCKGO: 30,
        SearchProvider.BRAVE: 60,
        SearchProvider.BING: 100,
        SearchProvider.GOOGLE: 100,
        SearchProvider.SEARXNG: 120,
    })

    # Caching
    cache_ttl_seconds: int = 3600  # 1 hour
    max_cache_size: int = 10000

    # Retry configuration
    max_retries: int = 3
    retry_base_delay: float = 1.0
    retry_max_delay: float = 30.0
    retry_exponential_base: float = 2.0

    # Circuit breaker
    circuit_breaker_threshold: int = 5  # failures before opening
    circuit_breaker_timeout: float = 60.0  # seconds to wait before retry

    # Timeouts
    request_timeout: float = 30.0
    connection_timeout: float = 10.0

    # Result limits
    max_results_per_query: int = 20
    max_parallel_requests: int = 5

    # API Keys (loaded from environment)
    brave_api_key: Optional[str] = None
    bing_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    google_cx_id: Optional[str] = None
    github_token: Optional[str] = None
    stackoverflow_key: Optional[str] = None

    def __post_init__(self):
        """Load API keys from environment."""
        self.brave_api_key = os.getenv("BRAVE_SEARCH_API_KEY", self.brave_api_key)
        self.bing_api_key = os.getenv("BING_SEARCH_API_KEY", self.bing_api_key)
        self.google_api_key = os.getenv("GOOGLE_SEARCH_API_KEY", self.google_api_key)
        self.google_cx_id = os.getenv("GOOGLE_SEARCH_CX_ID", self.google_cx_id)
        self.github_token = os.getenv("GITHUB_TOKEN", self.github_token)
        self.stackoverflow_key = os.getenv("STACKOVERFLOW_API_KEY", self.stackoverflow_key)


class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, rate_per_minute: int):
        self.rate = rate_per_minute
        self.tokens = float(rate_per_minute)
        self.max_tokens = float(rate_per_minute)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        """Acquire a token, waiting if necessary."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now

            # Replenish tokens
            self.tokens = min(
                self.max_tokens,
                self.tokens + elapsed * (self.rate / 60.0)
            )

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True

            # Calculate wait time
            wait_time = (1.0 - self.tokens) * (60.0 / self.rate)
            await asyncio.sleep(wait_time)
            self.tokens = 0.0
            return True

    def available(self) -> float:
        """Get available tokens without consuming."""
        now = time.monotonic()
        elapsed = now - self.last_update
        return min(
            self.max_tokens,
            self.tokens + elapsed * (self.rate / 60.0)
        )


class CircuitBreaker:
    """Circuit breaker for handling provider failures."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = 0
        self.last_failure_time: Optional[float] = None
        self.state = "closed"  # closed, open, half-open
        self._lock = asyncio.Lock()

    async def call(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with circuit breaker protection."""
        async with self._lock:
            if self.state == "open":
                if self.last_failure_time:
                    elapsed = time.monotonic() - self.last_failure_time
                    if elapsed >= self.recovery_timeout:
                        self.state = "half-open"
                    else:
                        raise Exception(
                            f"Circuit breaker open, retry in {self.recovery_timeout - elapsed:.1f}s"
                        )

        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            async with self._lock:
                self.failures = 0
                self.state = "closed"

            return result

        except Exception as e:
            async with self._lock:
                self.failures += 1
                self.last_failure_time = time.monotonic()

                if self.failures >= self.failure_threshold:
                    self.state = "open"
                    logger.warning(
                        f"Circuit breaker opened after {self.failures} failures"
                    )
            raise

    @property
    def is_open(self) -> bool:
        """Check if circuit is open."""
        return self.state == "open"


class SearchCache:
    """LRU cache with TTL for search results."""

    def __init__(self, max_size: int = 10000, ttl_seconds: int = 3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[Any, datetime]] = {}
        self._access_order: List[str] = []
        self._lock = asyncio.Lock()

    def _make_key(self, query: str, provider: Optional[str] = None) -> str:
        """Generate cache key."""
        key_data = f"{query}:{provider or 'default'}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:32]

    async def get(self, query: str, provider: Optional[str] = None) -> Optional[Any]:
        """Get cached result if not expired."""
        async with self._lock:
            key = self._make_key(query, provider)
            if key in self._cache:
                value, timestamp = self._cache[key]
                if (datetime.now() - timestamp).total_seconds() < self.ttl_seconds:
                    # Move to end of access order
                    if key in self._access_order:
                        self._access_order.remove(key)
                    self._access_order.append(key)
                    return value
                else:
                    # Expired, remove
                    del self._cache[key]
                    if key in self._access_order:
                        self._access_order.remove(key)
            return None

    async def set(self, query: str, value: Any, provider: Optional[str] = None) -> None:
        """Cache a result."""
        async with self._lock:
            key = self._make_key(query, provider)

            # Evict if at capacity
            while len(self._cache) >= self.max_size and self._access_order:
                oldest_key = self._access_order.pop(0)
                if oldest_key in self._cache:
                    del self._cache[oldest_key]

            self._cache[key] = (value, datetime.now())
            self._access_order.append(key)

    async def invalidate(self, query: str, provider: Optional[str] = None) -> None:
        """Invalidate a cached entry."""
        async with self._lock:
            key = self._make_key(query, provider)
            if key in self._cache:
                del self._cache[key]
            if key in self._access_order:
                self._access_order.remove(key)

    async def clear(self) -> None:
        """Clear all cached entries."""
        async with self._lock:
            self._cache.clear()
            self._access_order.clear()

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "ttl_seconds": self.ttl_seconds,
        }


class WebSearchExtractor:
    """
    Multi-provider web search extractor with fallback, rate limiting, and caching.

    Supports:
    - DuckDuckGo (free, no API key required)
    - Brave Search (API key optional for limited use)
    - Bing Search (API key required)
    - Google Custom Search (API key required)
    - SearXNG (self-hosted, configurable)
    """

    def __init__(self, config: Optional[WebSearchConfig] = None):
        self.config = config or WebSearchConfig()
        self._cache = SearchCache(
            max_size=self.config.max_cache_size,
            ttl_seconds=self.config.cache_ttl_seconds,
        )
        self._rate_limiters: Dict[SearchProvider, RateLimiter] = {}
        self._circuit_breakers: Dict[SearchProvider, CircuitBreaker] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._initialized = False

        # Initialize rate limiters and circuit breakers for each provider
        for provider, rate in self.config.rate_limits.items():
            self._rate_limiters[provider] = RateLimiter(rate)
            self._circuit_breakers[provider] = CircuitBreaker(
                failure_threshold=self.config.circuit_breaker_threshold,
                recovery_timeout=self.config.circuit_breaker_timeout,
            )

    async def initialize(self) -> None:
        """Initialize HTTP session and validate configuration."""
        if self._initialized:
            return

        connector = aiohttp.TCPConnector(
            limit=self.config.max_parallel_requests,
            limit_per_host=10,
        )

        timeout = aiohttp.ClientTimeout(
            total=self.config.request_timeout,
            connect=self.config.connection_timeout,
        )

        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": "JARVIS-AI-Agent/1.0 (Self-Programming System)"},
        )

        self._initialized = True
        logger.info("WebSearchExtractor initialized")

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None
        self._initialized = False
        logger.info("WebSearchExtractor shutdown complete")

    async def search(
        self,
        query: str,
        max_results: int = 10,
        result_types: Optional[List[SearchResultType]] = None,
        use_cache: bool = True,
    ) -> List[SearchResult]:
        """
        Perform web search across providers with fallback.

        Args:
            query: Search query string
            max_results: Maximum results to return
            result_types: Filter by result types (None = all)
            use_cache: Whether to use cached results

        Returns:
            List of search results sorted by relevance
        """
        if not self._initialized:
            await self.initialize()

        # Check cache first
        if use_cache:
            cached = await self._cache.get(query)
            if cached:
                logger.debug(f"Cache hit for query: {query[:50]}...")
                results = cached
                for r in results:
                    r.cached = True
                return results[:max_results]

        # Try providers in order
        providers = [self.config.primary_provider] + self.config.fallback_providers
        last_error = None

        for provider in providers:
            if self._circuit_breakers[provider].is_open:
                logger.debug(f"Skipping {provider.value} - circuit breaker open")
                continue

            try:
                # Rate limit
                await self._rate_limiters[provider].acquire()

                # Execute search with circuit breaker
                results = await self._circuit_breakers[provider].call(
                    self._search_provider,
                    provider,
                    query,
                    max_results * 2,  # Get extra for filtering
                )

                # Cache results
                await self._cache.set(query, results)

                # Filter and sort
                if result_types:
                    results = [r for r in results if r.result_type in result_types]

                results.sort(key=lambda r: r.relevance_score, reverse=True)

                logger.info(
                    f"Search '{query[:30]}...' returned {len(results)} results from {provider.value}"
                )
                return results[:max_results]

            except Exception as e:
                logger.warning(f"Search failed with {provider.value}: {e}")
                last_error = e
                continue

        # All providers failed
        if last_error:
            logger.error(f"All search providers failed for query: {query}")
            raise last_error

        return []

    async def _search_provider(
        self,
        provider: SearchProvider,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Execute search on specific provider."""
        if provider == SearchProvider.DUCKDUCKGO:
            return await self._search_duckduckgo(query, max_results)
        elif provider == SearchProvider.BRAVE:
            return await self._search_brave(query, max_results)
        elif provider == SearchProvider.BING:
            return await self._search_bing(query, max_results)
        elif provider == SearchProvider.GOOGLE:
            return await self._search_google(query, max_results)
        elif provider == SearchProvider.SEARXNG:
            return await self._search_searxng(query, max_results)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    async def _search_duckduckgo(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Search using DuckDuckGo HTML (no API key required)."""
        results = []

        # Use DuckDuckGo Lite (HTML) for reliability
        url = "https://lite.duckduckgo.com/lite/"
        params = {"q": query, "kl": "us-en"}

        async with self._session.post(url, data=params) as response:
            response.raise_for_status()
            html = await response.text()

        # Parse HTML results
        # DuckDuckGo Lite uses simple HTML structure
        soup = None
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
        except ImportError:
            # Fallback to regex parsing
            import re

            # Extract links from HTML
            link_pattern = r'<a[^>]+href="([^"]+)"[^>]*class="result-link"[^>]*>([^<]+)</a>'
            snippet_pattern = r'<td[^>]+class="result-snippet"[^>]*>([^<]+)</td>'

            links = re.findall(link_pattern, html)
            snippets = re.findall(snippet_pattern, html)

            for i, (link_url, title) in enumerate(links[:max_results]):
                snippet = snippets[i] if i < len(snippets) else ""
                results.append(SearchResult(
                    title=title.strip(),
                    url=link_url,
                    snippet=snippet.strip(),
                    provider=SearchProvider.DUCKDUCKGO,
                    result_type=self._classify_result_type(link_url, title),
                    relevance_score=1.0 - (i * 0.05),
                ))

        if soup:
            # Parse with BeautifulSoup
            for i, link in enumerate(soup.select("a.result-link")[:max_results]):
                link_url = link.get("href", "")
                title = link.get_text(strip=True)

                # Get snippet from sibling
                snippet_td = link.find_parent("tr")
                snippet = ""
                if snippet_td:
                    snippet_elem = snippet_td.find_next_sibling("tr")
                    if snippet_elem:
                        snippet = snippet_elem.get_text(strip=True)

                results.append(SearchResult(
                    title=title,
                    url=link_url,
                    snippet=snippet,
                    provider=SearchProvider.DUCKDUCKGO,
                    result_type=self._classify_result_type(link_url, title),
                    relevance_score=1.0 - (i * 0.05),
                ))

        return results

    async def _search_brave(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Search using Brave Search API."""
        if not self.config.brave_api_key:
            raise ValueError("Brave API key not configured")

        results = []
        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "X-Subscription-Token": self.config.brave_api_key,
            "Accept": "application/json",
        }
        params = {
            "q": query,
            "count": min(max_results, 20),
            "freshness": "py",  # Past year
        }

        async with self._session.get(url, headers=headers, params=params) as response:
            response.raise_for_status()
            data = await response.json()

        web_results = data.get("web", {}).get("results", [])

        for i, item in enumerate(web_results):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                provider=SearchProvider.BRAVE,
                result_type=self._classify_result_type(
                    item.get("url", ""),
                    item.get("title", ""),
                ),
                relevance_score=1.0 - (i * 0.04),
                metadata={
                    "published": item.get("age"),
                    "family_friendly": item.get("family_friendly"),
                },
            ))

        return results

    async def _search_bing(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Search using Bing Web Search API."""
        if not self.config.bing_api_key:
            raise ValueError("Bing API key not configured")

        results = []
        url = "https://api.bing.microsoft.com/v7.0/search"
        headers = {"Ocp-Apim-Subscription-Key": self.config.bing_api_key}
        params = {
            "q": query,
            "count": min(max_results, 50),
            "mkt": "en-US",
        }

        async with self._session.get(url, headers=headers, params=params) as response:
            response.raise_for_status()
            data = await response.json()

        web_pages = data.get("webPages", {}).get("value", [])

        for i, item in enumerate(web_pages):
            results.append(SearchResult(
                title=item.get("name", ""),
                url=item.get("url", ""),
                snippet=item.get("snippet", ""),
                provider=SearchProvider.BING,
                result_type=self._classify_result_type(
                    item.get("url", ""),
                    item.get("name", ""),
                ),
                relevance_score=1.0 - (i * 0.03),
                metadata={
                    "date_last_crawled": item.get("dateLastCrawled"),
                    "language": item.get("language"),
                },
            ))

        return results

    async def _search_google(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Search using Google Custom Search API."""
        if not self.config.google_api_key or not self.config.google_cx_id:
            raise ValueError("Google API key or CX ID not configured")

        results = []
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": self.config.google_api_key,
            "cx": self.config.google_cx_id,
            "q": query,
            "num": min(max_results, 10),
        }

        async with self._session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()

        items = data.get("items", [])

        for i, item in enumerate(items):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                provider=SearchProvider.GOOGLE,
                result_type=self._classify_result_type(
                    item.get("link", ""),
                    item.get("title", ""),
                ),
                relevance_score=1.0 - (i * 0.05),
                metadata={
                    "display_link": item.get("displayLink"),
                    "cache_id": item.get("cacheId"),
                },
            ))

        return results

    async def _search_searxng(
        self,
        query: str,
        max_results: int,
    ) -> List[SearchResult]:
        """Search using self-hosted SearXNG instance."""
        searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8888")

        results = []
        url = f"{searxng_url}/search"
        params = {
            "q": query,
            "format": "json",
            "engines": "google,bing,duckduckgo",
        }

        async with self._session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()

        for i, item in enumerate(data.get("results", [])[:max_results]):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                provider=SearchProvider.SEARXNG,
                result_type=self._classify_result_type(
                    item.get("url", ""),
                    item.get("title", ""),
                ),
                relevance_score=item.get("score", 1.0 - (i * 0.05)),
                metadata={
                    "engine": item.get("engine"),
                    "parsed_url": item.get("parsed_url"),
                },
            ))

        return results

    def _classify_result_type(self, url: str, title: str) -> SearchResultType:
        """Classify search result type based on URL and title."""
        url_lower = url.lower()
        title_lower = title.lower()

        # Documentation sites
        doc_patterns = [
            "docs.", "documentation", ".readthedocs.", "/docs/",
            "developer.", "reference/", "api-reference",
        ]
        if any(p in url_lower for p in doc_patterns):
            return SearchResultType.DOCUMENTATION

        # Stack Overflow
        if "stackoverflow.com" in url_lower or "stackexchange.com" in url_lower:
            return SearchResultType.STACK_OVERFLOW

        # GitHub
        if "github.com" in url_lower:
            if "/blob/" in url_lower or "/tree/" in url_lower:
                return SearchResultType.GITHUB_CODE
            return SearchResultType.GITHUB_REPO

        # API references
        if "api" in url_lower and ("reference" in url_lower or "docs" in url_lower):
            return SearchResultType.API_REFERENCE

        # Tutorials
        tutorial_patterns = ["tutorial", "guide", "how-to", "getting-started"]
        if any(p in url_lower or p in title_lower for p in tutorial_patterns):
            return SearchResultType.TUTORIAL

        # Blog posts
        blog_patterns = ["blog", "medium.com", "dev.to", "hashnode"]
        if any(p in url_lower for p in blog_patterns):
            return SearchResultType.BLOG_POST

        return SearchResultType.WEB_PAGE

    async def search_for_error(
        self,
        error_message: str,
        language: str = "python",
        max_results: int = 10,
    ) -> List[SearchResult]:
        """Search specifically for error solutions."""
        # Construct targeted query
        query = f"{language} {error_message} solution"

        results = await self.search(
            query,
            max_results=max_results * 2,
            result_types=[
                SearchResultType.STACK_OVERFLOW,
                SearchResultType.DOCUMENTATION,
                SearchResultType.BLOG_POST,
            ],
        )

        # Boost Stack Overflow results
        for r in results:
            if r.result_type == SearchResultType.STACK_OVERFLOW:
                r.relevance_score *= 1.5

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:max_results]

    async def search_for_library(
        self,
        library_name: str,
        topic: str = "usage",
        max_results: int = 10,
    ) -> List[SearchResult]:
        """Search for library documentation and usage."""
        query = f"{library_name} {topic} documentation"

        results = await self.search(
            query,
            max_results=max_results * 2,
            result_types=[
                SearchResultType.DOCUMENTATION,
                SearchResultType.API_REFERENCE,
                SearchResultType.TUTORIAL,
            ],
        )

        # Boost official docs
        for r in results:
            if "official" in r.title.lower() or library_name.lower() in r.url.lower():
                r.relevance_score *= 1.3

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:max_results]


class DocumentationLookup:
    """
    Library documentation lookup with official docs priority.

    Maintains mapping of popular libraries to their official documentation URLs
    and provides structured documentation retrieval.
    """

    # Dynamic documentation source registry
    DOCUMENTATION_SOURCES: Dict[str, Dict[str, str]] = {
        # Python standard library and popular packages
        "python": {"base": "https://docs.python.org/3/", "search": "search.html?q="},
        "asyncio": {"base": "https://docs.python.org/3/library/asyncio.html"},
        "typing": {"base": "https://docs.python.org/3/library/typing.html"},

        # Web frameworks
        "fastapi": {"base": "https://fastapi.tiangolo.com/", "search": "?q="},
        "flask": {"base": "https://flask.palletsprojects.com/", "search": "search/?q="},
        "django": {"base": "https://docs.djangoproject.com/", "search": "search/?q="},
        "starlette": {"base": "https://www.starlette.io/"},

        # Async/networking
        "aiohttp": {"base": "https://docs.aiohttp.org/en/stable/"},
        "httpx": {"base": "https://www.python-httpx.org/"},
        "requests": {"base": "https://requests.readthedocs.io/en/latest/"},

        # Data science
        "numpy": {"base": "https://numpy.org/doc/stable/", "search": "search.html?q="},
        "pandas": {"base": "https://pandas.pydata.org/docs/", "search": "search.html?q="},
        "scipy": {"base": "https://docs.scipy.org/doc/scipy/", "search": "search.html?q="},
        "matplotlib": {"base": "https://matplotlib.org/stable/", "search": "search.html?q="},

        # ML/AI
        "torch": {"base": "https://pytorch.org/docs/stable/", "search": "search.html?q="},
        "pytorch": {"base": "https://pytorch.org/docs/stable/", "search": "search.html?q="},
        "tensorflow": {"base": "https://www.tensorflow.org/api_docs/python/"},
        "transformers": {"base": "https://huggingface.co/docs/transformers/"},
        "langchain": {"base": "https://python.langchain.com/docs/"},
        "openai": {"base": "https://platform.openai.com/docs/"},
        "anthropic": {"base": "https://docs.anthropic.com/"},

        # Database
        "sqlalchemy": {"base": "https://docs.sqlalchemy.org/en/20/"},
        "psycopg2": {"base": "https://www.psycopg.org/docs/"},
        "redis": {"base": "https://redis.io/docs/"},
        "chromadb": {"base": "https://docs.trychroma.com/"},

        # Testing
        "pytest": {"base": "https://docs.pytest.org/en/stable/", "search": "search.html?q="},
        "unittest": {"base": "https://docs.python.org/3/library/unittest.html"},

        # DevOps/Cloud
        "docker": {"base": "https://docs.docker.com/"},
        "kubernetes": {"base": "https://kubernetes.io/docs/"},
        "gcp": {"base": "https://cloud.google.com/docs/"},
        "aws": {"base": "https://docs.aws.amazon.com/"},

        # Misc
        "pydantic": {"base": "https://docs.pydantic.dev/latest/"},
        "click": {"base": "https://click.palletsprojects.com/"},
        "rich": {"base": "https://rich.readthedocs.io/en/stable/"},
    }

    def __init__(
        self,
        search_extractor: Optional[WebSearchExtractor] = None,
        config: Optional[WebSearchConfig] = None,
    ):
        self.search_extractor = search_extractor
        self.config = config or WebSearchConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._cache = SearchCache(
            max_size=self.config.max_cache_size,
            ttl_seconds=self.config.cache_ttl_seconds * 2,  # Docs change less often
        )

    async def initialize(self) -> None:
        """Initialize HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers={"User-Agent": "JARVIS-AI-Agent/1.0 (Documentation Lookup)"},
            )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None

    def register_documentation_source(
        self,
        library: str,
        base_url: str,
        search_path: Optional[str] = None,
    ) -> None:
        """Dynamically register a new documentation source."""
        self.DOCUMENTATION_SOURCES[library.lower()] = {
            "base": base_url,
            **({"search": search_path} if search_path else {}),
        }

    async def lookup(
        self,
        library: str,
        topic: Optional[str] = None,
        version: Optional[str] = None,
    ) -> List[DocumentationResult]:
        """
        Look up documentation for a library.

        Args:
            library: Library name (e.g., 'fastapi', 'numpy')
            topic: Specific topic to search for
            version: Specific version (if applicable)

        Returns:
            List of documentation results
        """
        await self.initialize()

        library_lower = library.lower()
        results = []

        # Check if we have a known documentation source
        if library_lower in self.DOCUMENTATION_SOURCES:
            source = self.DOCUMENTATION_SOURCES[library_lower]
            base_url = source["base"]

            # Direct documentation URL
            results.append(DocumentationResult(
                library_name=library,
                version=version,
                doc_type=DocumentationType.OFFICIAL,
                title=f"{library} Official Documentation",
                url=base_url,
                content="",  # Will be fetched on demand
                relevance_score=1.0,
            ))

            # If topic specified and search path exists
            if topic and "search" in source:
                search_url = f"{base_url}{source['search']}{topic}"
                results.append(DocumentationResult(
                    library_name=library,
                    version=version,
                    doc_type=DocumentationType.OFFICIAL,
                    title=f"{library} - {topic}",
                    url=search_url,
                    content="",
                    relevance_score=0.95,
                ))

        # Fall back to web search if needed
        if self.search_extractor and (not results or topic):
            query = f"{library} {topic or 'documentation'} official docs"
            search_results = await self.search_extractor.search_for_library(
                library,
                topic=topic or "documentation",
                max_results=5,
            )

            for sr in search_results:
                if sr.result_type in [
                    SearchResultType.DOCUMENTATION,
                    SearchResultType.API_REFERENCE,
                ]:
                    results.append(DocumentationResult(
                        library_name=library,
                        version=version,
                        doc_type=self._classify_doc_type(sr),
                        title=sr.title,
                        url=sr.url,
                        content=sr.snippet,
                        relevance_score=sr.relevance_score * 0.9,
                    ))

        return results

    async def fetch_content(
        self,
        doc_result: DocumentationResult,
        extract_examples: bool = True,
    ) -> DocumentationResult:
        """
        Fetch and parse documentation content.

        Args:
            doc_result: Documentation result to fetch
            extract_examples: Whether to extract code examples

        Returns:
            Updated documentation result with content
        """
        await self.initialize()

        try:
            async with self._session.get(doc_result.url) as response:
                response.raise_for_status()
                html = await response.text()

            # Parse HTML
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")

                # Extract main content
                main_content = soup.find("main") or soup.find("article") or soup.find("body")
                if main_content:
                    doc_result.content = main_content.get_text(separator="\n", strip=True)

                # Extract code examples
                if extract_examples:
                    code_blocks = soup.find_all(["code", "pre"])
                    for block in code_blocks:
                        code = block.get_text(strip=True)
                        if len(code) > 20:  # Skip tiny snippets
                            doc_result.code_examples.append(code)

                # Extract related topics
                links = soup.find_all("a", href=True)
                for link in links[:20]:
                    href = link.get("href", "")
                    text = link.get_text(strip=True)
                    if href.startswith(doc_result.url) and text:
                        doc_result.related_topics.append(text)

            except ImportError:
                # Fallback: Basic content extraction
                import re
                # Remove HTML tags
                doc_result.content = re.sub(r"<[^>]+>", " ", html)
                doc_result.content = re.sub(r"\s+", " ", doc_result.content).strip()

                # Extract code blocks with regex
                code_pattern = r"<(?:code|pre)[^>]*>(.*?)</(?:code|pre)>"
                matches = re.findall(code_pattern, html, re.DOTALL | re.IGNORECASE)
                doc_result.code_examples = [
                    re.sub(r"<[^>]+>", "", m).strip()
                    for m in matches if len(m) > 20
                ]

        except Exception as e:
            logger.warning(f"Failed to fetch documentation: {e}")

        return doc_result

    def _classify_doc_type(self, search_result: SearchResult) -> DocumentationType:
        """Classify documentation type from search result."""
        url_lower = search_result.url.lower()
        title_lower = search_result.title.lower()

        if "api" in url_lower or "api" in title_lower:
            return DocumentationType.API_REFERENCE
        elif "tutorial" in url_lower or "tutorial" in title_lower:
            return DocumentationType.TUTORIAL
        elif "changelog" in url_lower or "release" in url_lower:
            return DocumentationType.CHANGELOG
        elif "readme" in url_lower:
            return DocumentationType.README
        elif any(x in url_lower for x in ["docs.", "documentation", ".readthedocs."]):
            return DocumentationType.OFFICIAL
        else:
            return DocumentationType.COMMUNITY


class StackOverflowSearcher:
    """
    Stack Overflow integration for finding solutions to errors and coding questions.

    Supports both API and web scraping fallback.
    """

    API_BASE = "https://api.stackexchange.com/2.3"
    SITE = "stackoverflow"

    def __init__(self, config: Optional[WebSearchConfig] = None):
        self.config = config or WebSearchConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._rate_limiter = RateLimiter(30)  # SO API limit
        self._cache = SearchCache(
            max_size=self.config.max_cache_size,
            ttl_seconds=self.config.cache_ttl_seconds,
        )

    async def initialize(self) -> None:
        """Initialize HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None

    async def search(
        self,
        query: str,
        tags: Optional[List[str]] = None,
        max_results: int = 10,
        sort: str = "relevance",
    ) -> List[StackOverflowResult]:
        """
        Search Stack Overflow for questions matching query.

        Args:
            query: Search query
            tags: Filter by tags (e.g., ['python', 'asyncio'])
            max_results: Maximum results to return
            sort: Sort order ('relevance', 'votes', 'creation', 'activity')

        Returns:
            List of Stack Overflow results
        """
        await self.initialize()

        # Check cache
        cache_key = f"{query}:{','.join(tags or [])}:{sort}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached[:max_results]

        await self._rate_limiter.acquire()

        results = []

        # Build API request
        params = {
            "order": "desc",
            "sort": sort,
            "intitle": query,
            "site": self.SITE,
            "pagesize": min(max_results, 30),
            "filter": "withbody",  # Include question body
        }

        if tags:
            params["tagged"] = ";".join(tags)

        if self.config.stackoverflow_key:
            params["key"] = self.config.stackoverflow_key

        try:
            async with self._session.get(
                f"{self.API_BASE}/search/advanced",
                params=params,
            ) as response:
                response.raise_for_status()
                data = await response.json()

            for item in data.get("items", []):
                result = StackOverflowResult(
                    question_id=item["question_id"],
                    question_title=item["title"],
                    question_url=item["link"],
                    question_score=item["score"],
                    answer_count=item["answer_count"],
                    is_answered=item["is_answered"],
                    accepted_answer_id=item.get("accepted_answer_id"),
                    tags=item.get("tags", []),
                    question_body=item.get("body", ""),
                    relevance_score=self._calculate_relevance(item, query),
                )
                results.append(result)

            # Cache results
            await self._cache.set(cache_key, results)

        except Exception as e:
            logger.warning(f"Stack Overflow API search failed: {e}")
            # Fallback could be implemented here

        return results[:max_results]

    async def get_answers(
        self,
        question_id: int,
        max_answers: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Get answers for a specific question.

        Args:
            question_id: Stack Overflow question ID
            max_answers: Maximum answers to fetch

        Returns:
            List of answer dictionaries
        """
        await self.initialize()
        await self._rate_limiter.acquire()

        params = {
            "order": "desc",
            "sort": "votes",
            "site": self.SITE,
            "pagesize": max_answers,
            "filter": "withbody",
        }

        if self.config.stackoverflow_key:
            params["key"] = self.config.stackoverflow_key

        try:
            async with self._session.get(
                f"{self.API_BASE}/questions/{question_id}/answers",
                params=params,
            ) as response:
                response.raise_for_status()
                data = await response.json()

            return [
                {
                    "answer_id": a["answer_id"],
                    "score": a["score"],
                    "is_accepted": a.get("is_accepted", False),
                    "body": a.get("body", ""),
                    "creation_date": a.get("creation_date"),
                }
                for a in data.get("items", [])
            ]

        except Exception as e:
            logger.warning(f"Failed to fetch answers: {e}")
            return []

    async def search_for_error(
        self,
        error_message: str,
        language: str = "python",
        max_results: int = 5,
    ) -> List[StackOverflowResult]:
        """
        Search for solutions to a specific error.

        Args:
            error_message: The error message to search for
            language: Programming language tag
            max_results: Maximum results

        Returns:
            List of relevant Stack Overflow results
        """
        # Clean up error message for search
        import re

        # Remove file paths and line numbers
        clean_error = re.sub(r'File "[^"]+", line \d+', '', error_message)
        # Remove memory addresses
        clean_error = re.sub(r'0x[a-fA-F0-9]+', '', clean_error)
        # Get core error type and message
        clean_error = clean_error.strip()[:200]

        results = await self.search(
            query=clean_error,
            tags=[language],
            max_results=max_results,
            sort="relevance",
        )

        # Fetch answers for top results
        for result in results[:3]:
            result.answers = await self.get_answers(
                result.question_id,
                max_answers=3,
            )

        return results

    def _calculate_relevance(
        self,
        item: Dict[str, Any],
        query: str,
    ) -> float:
        """Calculate relevance score for a result."""
        score = 0.5  # Base score

        # Boost for answered questions
        if item.get("is_answered"):
            score += 0.2

        # Boost for accepted answers
        if item.get("accepted_answer_id"):
            score += 0.1

        # Boost for high vote count
        votes = item.get("score", 0)
        if votes > 100:
            score += 0.15
        elif votes > 50:
            score += 0.1
        elif votes > 10:
            score += 0.05

        # Title match bonus
        title_lower = item.get("title", "").lower()
        query_words = query.lower().split()
        matching_words = sum(1 for w in query_words if w in title_lower)
        score += min(0.15, matching_words * 0.03)

        return min(1.0, score)


class CodeExampleScraper:
    """
    Code example scraper for GitHub repositories and gists.

    Finds relevant code examples for implementation patterns.
    """

    GITHUB_API = "https://api.github.com"

    def __init__(self, config: Optional[WebSearchConfig] = None):
        self.config = config or WebSearchConfig()
        self._session: Optional[aiohttp.ClientSession] = None
        self._rate_limiter = RateLimiter(30)  # GitHub API limit
        self._cache = SearchCache(
            max_size=self.config.max_cache_size,
            ttl_seconds=self.config.cache_ttl_seconds * 2,
        )

    async def initialize(self) -> None:
        """Initialize HTTP session."""
        if self._session is None:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "JARVIS-AI-Agent/1.0",
            }
            if self.config.github_token:
                headers["Authorization"] = f"token {self.config.github_token}"

            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers,
            )

    async def shutdown(self) -> None:
        """Cleanup resources."""
        if self._session:
            await self._session.close()
            self._session = None

    async def search_code(
        self,
        query: str,
        language: str = "python",
        max_results: int = 10,
    ) -> List[CodeExample]:
        """
        Search GitHub for code examples.

        Args:
            query: Code search query
            language: Programming language filter
            max_results: Maximum results

        Returns:
            List of code examples
        """
        await self.initialize()

        # Check cache
        cache_key = f"{query}:{language}"
        cached = await self._cache.get(cache_key)
        if cached:
            return cached[:max_results]

        await self._rate_limiter.acquire()

        results = []

        # Build search query
        search_query = f"{query} language:{language}"

        params = {
            "q": search_query,
            "per_page": min(max_results, 30),
            "sort": "indexed",
        }

        try:
            async with self._session.get(
                f"{self.GITHUB_API}/search/code",
                params=params,
            ) as response:
                if response.status == 403:
                    # Rate limited
                    logger.warning("GitHub API rate limited")
                    return []

                response.raise_for_status()
                data = await response.json()

            for item in data.get("items", []):
                # Fetch file content
                content = await self._fetch_file_content(item["url"])

                if content:
                    results.append(CodeExample(
                        source="github",
                        repo_name=item["repository"]["full_name"],
                        file_path=item["path"],
                        code=content,
                        language=language,
                        context=item.get("name", ""),
                        stars=0,  # Would need additional API call
                        url=item["html_url"],
                        relevance_score=item.get("score", 0.5),
                    ))

            # Cache results
            await self._cache.set(cache_key, results)

        except Exception as e:
            logger.warning(f"GitHub code search failed: {e}")

        return results[:max_results]

    async def search_repos(
        self,
        query: str,
        language: str = "python",
        max_results: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Search for GitHub repositories.

        Args:
            query: Repository search query
            language: Language filter
            max_results: Maximum results

        Returns:
            List of repository info dictionaries
        """
        await self.initialize()
        await self._rate_limiter.acquire()

        params = {
            "q": f"{query} language:{language}",
            "sort": "stars",
            "per_page": max_results,
        }

        try:
            async with self._session.get(
                f"{self.GITHUB_API}/search/repositories",
                params=params,
            ) as response:
                response.raise_for_status()
                data = await response.json()

            return [
                {
                    "name": r["full_name"],
                    "description": r.get("description", ""),
                    "url": r["html_url"],
                    "stars": r["stargazers_count"],
                    "forks": r["forks_count"],
                    "language": r.get("language"),
                    "topics": r.get("topics", []),
                }
                for r in data.get("items", [])
            ]

        except Exception as e:
            logger.warning(f"GitHub repo search failed: {e}")
            return []

    async def get_readme(self, repo_name: str) -> Optional[str]:
        """
        Get README content for a repository.

        Args:
            repo_name: Repository full name (owner/repo)

        Returns:
            README content or None
        """
        await self.initialize()
        await self._rate_limiter.acquire()

        try:
            async with self._session.get(
                f"{self.GITHUB_API}/repos/{repo_name}/readme",
                headers={"Accept": "application/vnd.github.v3.raw"},
            ) as response:
                if response.status == 200:
                    return await response.text()
        except Exception as e:
            logger.debug(f"Failed to fetch README: {e}")

        return None

    async def _fetch_file_content(self, content_url: str) -> Optional[str]:
        """Fetch raw file content from GitHub API."""
        try:
            async with self._session.get(
                content_url,
                headers={"Accept": "application/vnd.github.v3.raw"},
            ) as response:
                if response.status == 200:
                    return await response.text()
        except Exception as e:
            logger.debug(f"Failed to fetch file content: {e}")

        return None


class OuroborosWebIntegration:
    """
    Integrates web search capabilities into the Ouroboros improvement loop.

    This class bridges the gap between autonomous self-programming and
    external knowledge sources, enabling:
    - Error resolution via Stack Overflow
    - Library documentation lookup during implementation
    - Code example discovery for implementation patterns
    - Best practices research before refactoring
    """

    def __init__(
        self,
        search_extractor: Optional[WebSearchExtractor] = None,
        doc_lookup: Optional[DocumentationLookup] = None,
        stackoverflow: Optional[StackOverflowSearcher] = None,
        code_scraper: Optional[CodeExampleScraper] = None,
        oracle: Optional[Any] = None,
        orchestrator: Optional[Any] = None,
        config: Optional[WebSearchConfig] = None,
    ):
        self.config = config or WebSearchConfig()

        # Initialize components
        self.search_extractor = search_extractor or WebSearchExtractor(self.config)
        self.doc_lookup = doc_lookup or DocumentationLookup(
            search_extractor=self.search_extractor,
            config=self.config,
        )
        self.stackoverflow = stackoverflow or StackOverflowSearcher(self.config)
        self.code_scraper = code_scraper or CodeExampleScraper(self.config)

        # Ouroboros integration
        self.oracle = oracle
        self.orchestrator = orchestrator

        # Event handlers
        self._error_handlers: List[Callable] = []
        self._improvement_handlers: List[Callable] = []

        # Metrics
        self._searches_performed = 0
        self._errors_resolved = 0
        self._docs_fetched = 0
        self._examples_found = 0

    async def initialize(self) -> None:
        """Initialize all web integration components."""
        await asyncio.gather(
            self.search_extractor.initialize(),
            self.doc_lookup.initialize(),
            self.stackoverflow.initialize(),
            self.code_scraper.initialize(),
        )
        logger.info("OuroborosWebIntegration initialized")

    async def shutdown(self) -> None:
        """Shutdown all components."""
        await asyncio.gather(
            self.search_extractor.shutdown(),
            self.doc_lookup.shutdown(),
            self.stackoverflow.shutdown(),
            self.code_scraper.shutdown(),
        )
        logger.info("OuroborosWebIntegration shutdown complete")

    def register_error_handler(self, handler: Callable) -> None:
        """Register handler for error resolution events."""
        self._error_handlers.append(handler)

    def register_improvement_handler(self, handler: Callable) -> None:
        """Register handler for improvement research events."""
        self._improvement_handlers.append(handler)

    async def resolve_error(
        self,
        error_message: str,
        error_type: str = "",
        file_path: Optional[str] = None,
        code_context: Optional[str] = None,
        language: str = "python",
    ) -> Dict[str, Any]:
        """
        Attempt to resolve an error using web resources.

        Args:
            error_message: The error message
            error_type: Error type (e.g., 'TypeError', 'ImportError')
            file_path: File where error occurred
            code_context: Surrounding code context
            language: Programming language

        Returns:
            Resolution result with solutions and confidence
        """
        self._searches_performed += 1

        result = {
            "error_message": error_message,
            "error_type": error_type,
            "solutions": [],
            "documentation": [],
            "code_examples": [],
            "confidence": 0.0,
            "resolution_status": "pending",
        }

        # Parallel search across sources
        tasks = [
            self.stackoverflow.search_for_error(error_message, language),
            self.search_extractor.search_for_error(error_message, language),
        ]

        # If we know the library involved, get docs
        detected_library = self._detect_library_from_error(error_message)
        if detected_library:
            tasks.append(self.doc_lookup.lookup(detected_library, error_type))

        search_results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process Stack Overflow results
        if not isinstance(search_results[0], Exception):
            so_results = search_results[0]
            for so in so_results:
                if so.is_answered and so.answers:
                    result["solutions"].append({
                        "source": "stackoverflow",
                        "question": so.question_title,
                        "url": so.question_url,
                        "score": so.question_score,
                        "answer": so.answers[0].get("body", "") if so.answers else "",
                        "relevance": so.relevance_score,
                    })

        # Process web search results
        if not isinstance(search_results[1], Exception):
            web_results = search_results[1]
            for wr in web_results:
                if wr.result_type == SearchResultType.DOCUMENTATION:
                    result["documentation"].append({
                        "title": wr.title,
                        "url": wr.url,
                        "snippet": wr.snippet,
                        "relevance": wr.relevance_score,
                    })

        # Process documentation results
        if len(search_results) > 2 and not isinstance(search_results[2], Exception):
            doc_results = search_results[2]
            for doc in doc_results:
                result["documentation"].append({
                    "library": doc.library_name,
                    "title": doc.title,
                    "url": doc.url,
                    "type": doc.doc_type.value,
                    "relevance": doc.relevance_score,
                })

        # Calculate confidence
        if result["solutions"]:
            best_solution = max(result["solutions"], key=lambda s: s["relevance"])
            result["confidence"] = best_solution["relevance"]
            result["best_solution"] = best_solution

            if result["confidence"] > 0.7:
                result["resolution_status"] = "likely_resolved"
                self._errors_resolved += 1
            else:
                result["resolution_status"] = "needs_review"
        else:
            result["resolution_status"] = "no_solution_found"

        # Notify handlers
        for handler in self._error_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(result)
                else:
                    handler(result)
            except Exception as e:
                logger.warning(f"Error handler failed: {e}")

        return result

    async def research_improvement(
        self,
        topic: str,
        improvement_type: str = "refactoring",
        current_implementation: Optional[str] = None,
        target_libraries: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Research best practices and patterns for an improvement task.

        Args:
            topic: The improvement topic (e.g., 'async error handling')
            improvement_type: Type of improvement (refactoring, optimization, etc.)
            current_implementation: Current code for context
            target_libraries: Specific libraries to research

        Returns:
            Research results with patterns, examples, and recommendations
        """
        self._searches_performed += 1

        result = {
            "topic": topic,
            "improvement_type": improvement_type,
            "best_practices": [],
            "patterns": [],
            "code_examples": [],
            "documentation": [],
            "recommendations": [],
        }

        # Build search queries
        queries = [
            f"python {topic} best practices",
            f"python {improvement_type} {topic} pattern",
        ]

        if target_libraries:
            for lib in target_libraries:
                queries.append(f"{lib} {topic} example")

        # Execute parallel searches
        search_tasks = [
            self.search_extractor.search(q, max_results=5)
            for q in queries[:3]  # Limit parallel queries
        ]

        # Also search for code examples
        code_task = self.code_scraper.search_code(
            topic,
            language="python",
            max_results=5,
        )
        search_tasks.append(code_task)

        # Get documentation if libraries specified
        doc_tasks = []
        if target_libraries:
            for lib in target_libraries[:3]:
                doc_tasks.append(self.doc_lookup.lookup(lib, topic))

        all_results = await asyncio.gather(
            *search_tasks,
            *doc_tasks,
            return_exceptions=True,
        )

        # Process search results
        for i, res in enumerate(all_results[:len(search_tasks)]):
            if isinstance(res, Exception):
                continue

            if isinstance(res, list):
                if res and isinstance(res[0], CodeExample):
                    # Code examples
                    for ex in res:
                        result["code_examples"].append({
                            "source": ex.source,
                            "repo": ex.repo_name,
                            "path": ex.file_path,
                            "code": ex.code[:2000],  # Limit size
                            "url": ex.url,
                            "relevance": ex.relevance_score,
                        })
                        self._examples_found += 1
                else:
                    # Search results
                    for sr in res:
                        if sr.result_type == SearchResultType.TUTORIAL:
                            result["best_practices"].append({
                                "title": sr.title,
                                "url": sr.url,
                                "snippet": sr.snippet,
                            })
                        elif sr.result_type in [
                            SearchResultType.DOCUMENTATION,
                            SearchResultType.API_REFERENCE,
                        ]:
                            result["documentation"].append({
                                "title": sr.title,
                                "url": sr.url,
                                "snippet": sr.snippet,
                            })

        # Process documentation results
        for res in all_results[len(search_tasks):]:
            if isinstance(res, Exception):
                continue
            if isinstance(res, list):
                for doc in res:
                    result["documentation"].append({
                        "library": doc.library_name,
                        "title": doc.title,
                        "url": doc.url,
                        "type": doc.doc_type.value,
                    })
                    self._docs_fetched += 1

        # Generate recommendations based on findings
        result["recommendations"] = self._generate_recommendations(result)

        # Notify handlers
        for handler in self._improvement_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(result)
                else:
                    handler(result)
            except Exception as e:
                logger.warning(f"Improvement handler failed: {e}")

        return result

    async def get_library_usage(
        self,
        library: str,
        feature: str,
    ) -> Dict[str, Any]:
        """
        Get usage examples and documentation for a specific library feature.

        Args:
            library: Library name
            feature: Feature or function name

        Returns:
            Usage information with examples and documentation
        """
        result = {
            "library": library,
            "feature": feature,
            "documentation": [],
            "examples": [],
            "related_functions": [],
        }

        # Get documentation
        docs = await self.doc_lookup.lookup(library, feature)
        for doc in docs:
            fetched = await self.doc_lookup.fetch_content(doc)
            result["documentation"].append({
                "title": fetched.title,
                "url": fetched.url,
                "content": fetched.content[:3000],
                "examples": fetched.code_examples[:5],
            })

        # Get code examples
        examples = await self.code_scraper.search_code(
            f"{library} {feature}",
            max_results=5,
        )
        for ex in examples:
            result["examples"].append({
                "source": ex.source,
                "repo": ex.repo_name,
                "code": ex.code[:1500],
                "url": ex.url,
            })

        return result

    def _detect_library_from_error(self, error_message: str) -> Optional[str]:
        """Detect which library an error originated from."""
        # Common library error patterns
        patterns = {
            "aiohttp": ["aiohttp", "ClientSession", "ClientError"],
            "fastapi": ["fastapi", "HTTPException", "RequestValidation"],
            "pydantic": ["pydantic", "ValidationError", "validator"],
            "sqlalchemy": ["sqlalchemy", "IntegrityError", "OperationalError"],
            "requests": ["requests.exceptions", "ConnectionError", "HTTPError"],
            "numpy": ["numpy", "ndarray", "AxisError"],
            "pandas": ["pandas", "DataFrame", "KeyError"],
            "torch": ["torch", "cuda", "RuntimeError: CUDA"],
        }

        error_lower = error_message.lower()
        for lib, keywords in patterns.items():
            if any(kw.lower() in error_lower for kw in keywords):
                return lib

        return None

    def _generate_recommendations(
        self,
        research_result: Dict[str, Any],
    ) -> List[str]:
        """Generate recommendations based on research findings."""
        recommendations = []

        if research_result["code_examples"]:
            recommendations.append(
                f"Found {len(research_result['code_examples'])} relevant code examples. "
                "Review top-rated implementations for proven patterns."
            )

        if research_result["documentation"]:
            recommendations.append(
                f"Official documentation available for {len(research_result['documentation'])} topics. "
                "Consult docs before implementation."
            )

        if research_result["best_practices"]:
            recommendations.append(
                f"Found {len(research_result['best_practices'])} best practice guides. "
                "Consider established patterns."
            )

        if not any([
            research_result["code_examples"],
            research_result["documentation"],
            research_result["best_practices"],
        ]):
            recommendations.append(
                "Limited external resources found. Consider searching with different terms "
                "or consulting internal documentation."
            )

        return recommendations

    def get_metrics(self) -> Dict[str, Any]:
        """Get web integration metrics."""
        return {
            "searches_performed": self._searches_performed,
            "errors_resolved": self._errors_resolved,
            "docs_fetched": self._docs_fetched,
            "examples_found": self._examples_found,
            "resolution_rate": (
                self._errors_resolved / max(1, self._searches_performed)
            ),
        }


# =============================================================================
# Web Integration Factory Functions and Initialization
# =============================================================================

# Global instances
_web_search_extractor: Optional[WebSearchExtractor] = None
_doc_lookup: Optional[DocumentationLookup] = None
_stackoverflow_searcher: Optional[StackOverflowSearcher] = None
_code_example_scraper: Optional[CodeExampleScraper] = None
_ouroboros_web_integration: Optional[OuroborosWebIntegration] = None


def get_web_search_extractor(
    config: Optional[WebSearchConfig] = None,
) -> WebSearchExtractor:
    """Get or create the web search extractor."""
    global _web_search_extractor
    if _web_search_extractor is None:
        _web_search_extractor = WebSearchExtractor(config)
    return _web_search_extractor


def get_doc_lookup(
    search_extractor: Optional[WebSearchExtractor] = None,
    config: Optional[WebSearchConfig] = None,
) -> DocumentationLookup:
    """Get or create the documentation lookup."""
    global _doc_lookup
    if _doc_lookup is None:
        _doc_lookup = DocumentationLookup(
            search_extractor=search_extractor or get_web_search_extractor(config),
            config=config,
        )
    return _doc_lookup


def get_stackoverflow_searcher(
    config: Optional[WebSearchConfig] = None,
) -> StackOverflowSearcher:
    """Get or create the Stack Overflow searcher."""
    global _stackoverflow_searcher
    if _stackoverflow_searcher is None:
        _stackoverflow_searcher = StackOverflowSearcher(config)
    return _stackoverflow_searcher


def get_code_example_scraper(
    config: Optional[WebSearchConfig] = None,
) -> CodeExampleScraper:
    """Get or create the code example scraper."""
    global _code_example_scraper
    if _code_example_scraper is None:
        _code_example_scraper = CodeExampleScraper(config)
    return _code_example_scraper


def get_ouroboros_web_integration(
    oracle: Optional[Any] = None,
    orchestrator: Optional[Any] = None,
    config: Optional[WebSearchConfig] = None,
) -> OuroborosWebIntegration:
    """Get or create the Ouroboros web integration."""
    global _ouroboros_web_integration
    if _ouroboros_web_integration is None:
        _ouroboros_web_integration = OuroborosWebIntegration(
            search_extractor=get_web_search_extractor(config),
            doc_lookup=get_doc_lookup(config=config),
            stackoverflow=get_stackoverflow_searcher(config),
            code_scraper=get_code_example_scraper(config),
            oracle=oracle,
            orchestrator=orchestrator,
            config=config,
        )
    return _ouroboros_web_integration


async def initialize_web_integration(
    oracle: Optional[Any] = None,
    orchestrator: Optional[Any] = None,
    config: Optional[WebSearchConfig] = None,
) -> Dict[str, Any]:
    """
    Initialize all web integration components.

    Args:
        oracle: Oracle codebase knowledge graph
        orchestrator: AgenticLoopOrchestrator for task execution
        config: Web search configuration

    Returns:
        Dictionary with all initialized components
    """
    logger.info("Initializing web integration components...")

    web_integration = get_ouroboros_web_integration(
        oracle=oracle,
        orchestrator=orchestrator,
        config=config,
    )

    await web_integration.initialize()

    components = {
        "web_search_extractor": web_integration.search_extractor,
        "doc_lookup": web_integration.doc_lookup,
        "stackoverflow_searcher": web_integration.stackoverflow,
        "code_example_scraper": web_integration.code_scraper,
        "ouroboros_web_integration": web_integration,
    }

    logger.info(f"Web integration initialized with {len(components)} components")
    return components


async def shutdown_web_integration() -> None:
    """Shutdown all web integration components."""
    logger.info("Shutting down web integration components...")

    global _ouroboros_web_integration

    if _ouroboros_web_integration:
        await _ouroboros_web_integration.shutdown()

    # Reset global instances
    global _web_search_extractor, _doc_lookup, _stackoverflow_searcher, _code_example_scraper
    _web_search_extractor = None
    _doc_lookup = None
    _stackoverflow_searcher = None
    _code_example_scraper = None
    _ouroboros_web_integration = None

    logger.info("Web integration shutdown complete")

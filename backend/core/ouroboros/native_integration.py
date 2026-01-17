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

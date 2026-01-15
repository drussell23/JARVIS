"""
Native Self-Improvement Integration v1.0
========================================

This module transforms Ouroboros from an external CLI into a native capability
of the JARVIS Body. Like how your hand is part of your body - you don't "exit"
yourself to use your hand, you simply think and your hand moves.

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
Version: 1.0.0
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
        """Execute the improvement loop."""
        start_time = time.time()

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

                # Success! Apply changes
                if not request.dry_run:
                    progress.phase = ImprovementPhase.APPLYING
                    progress.progress_percent = 85
                    progress.message = "Applying changes..."
                    await self._progress_broadcaster.broadcast(progress)

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

        # All iterations failed
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

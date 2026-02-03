"""
Centralized Timeout Configuration for JARVIS Startup/Shutdown
===============================================================

This module provides a single source of truth for all timeout values used during
JARVIS startup, shutdown, and runtime operations. All timeouts are configurable
via environment variables with sensible defaults.

Design Principles:
- All env vars use JARVIS_ prefix for consistency
- Validation logs warnings but uses defaults (never crashes on bad config)
- MAX_TIMEOUT is configurable to support different deployment environments
- Each timeout is documented with its purpose and default

Environment Variables:
----------------------

### Global Timeout Configuration
- JARVIS_MAX_TIMEOUT: Maximum allowed timeout for any operation (default: 900.0s)
    Purpose: Safety cap to prevent unbounded waits

### Signal Timeouts (Shutdown)
- JARVIS_CLEANUP_TIMEOUT_SIGINT: Wait time after SIGINT before SIGTERM (default: 10.0s)
- JARVIS_CLEANUP_TIMEOUT_SIGTERM: Wait time after SIGTERM before SIGKILL (default: 5.0s)
- JARVIS_CLEANUP_TIMEOUT_SIGKILL: Wait time after SIGKILL before giving up (default: 2.0s)

### Port and Network Timeouts
- JARVIS_PORT_CHECK_TIMEOUT: Timeout for TCP port availability check (default: 1.0s)
- JARVIS_PORT_RELEASE_WAIT: Time to wait for port release after process exit (default: 2.0s)
- JARVIS_IPC_SOCKET_TIMEOUT: Timeout for Unix socket connections (default: 8.0s)

### Tool Timeouts
- JARVIS_LSOF_TIMEOUT: Timeout for lsof subprocess calls (default: 5.0s)
- JARVIS_DOCKER_CHECK_TIMEOUT: Timeout for docker health checks (default: 10.0s)

### Health Check Timeouts
- JARVIS_BACKEND_HEALTH_TIMEOUT: Timeout for backend HTTP health check (default: 30.0s)
- JARVIS_FRONTEND_HEALTH_TIMEOUT: Timeout for frontend health check (default: 60.0s)
- JARVIS_LOADING_SERVER_HEALTH_TIMEOUT: Timeout for loading server health (default: 5.0s)

### Heartbeat Configuration
- JARVIS_HEARTBEAT_INTERVAL: Interval between heartbeat broadcasts (default: 5.0s)

### Trinity Component Timeouts
- JARVIS_PRIME_STARTUP_TIMEOUT: Timeout for JARVIS-Prime startup (default: 600.0s)
- JARVIS_REACTOR_STARTUP_TIMEOUT: Timeout for Reactor-Core startup (default: 120.0s)
- JARVIS_REACTOR_HEALTH_TIMEOUT: Timeout for Reactor health check (default: 10.0s)

### Lock Timeouts
- JARVIS_STARTUP_LOCK_TIMEOUT: Timeout for acquiring startup lock (default: 30.0s)
- JARVIS_TAKEOVER_HANDOVER_TIMEOUT: Timeout for instance takeover handover (default: 15.0s)
- JARVIS_MAX_LOCK_TIMEOUT: Maximum allowed lock timeout (default: 300.0s)
- JARVIS_MIN_LOCK_TIMEOUT: Minimum allowed lock timeout (default: 0.1s)
- JARVIS_DEFAULT_LOCK_TIMEOUT: Default lock acquisition timeout (default: 5.0s)
- JARVIS_STALE_LOCK_RETRY_TIMEOUT: Timeout for retry after stale lock removal (default: 1.0s)

### Broadcast Timeout
- JARVIS_BROADCAST_TIMEOUT: Timeout for progress/status broadcasts (default: 2.0s)

### Async Utility Timeouts
- JARVIS_PROCESS_WAIT_TIMEOUT: Default timeout for async_process_wait (default: 10.0s)
- JARVIS_SUBPROCESS_TIMEOUT: Default timeout for async_subprocess_run (default: 30.0s)

Usage:
    from backend.config.startup_timeouts import StartupTimeouts

    timeouts = StartupTimeouts()

    # Access validated timeout values
    await async_process_wait(pid, timeout=timeouts.process_wait_timeout)

    # Or use the module-level singleton for convenience
    from backend.config.startup_timeouts import TIMEOUTS
    await sock.settimeout(TIMEOUTS.port_check_timeout)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# VALIDATION UTILITIES
# =============================================================================


def _get_env_float(
    name: str,
    default: float,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """
    Get a float value from environment variable with validation.

    Args:
        name: Environment variable name
        default: Default value if not set or invalid
        min_value: Minimum allowed value (inclusive)
        max_value: Maximum allowed value (inclusive)

    Returns:
        Validated float value (uses default on validation failure)
    """
    raw_value = os.environ.get(name)

    if raw_value is None:
        return default

    try:
        value = float(raw_value)
    except ValueError:
        logger.warning(
            f"[StartupTimeouts] Invalid value for {name}='{raw_value}' "
            f"(not a valid number), using default: {default}"
        )
        return default

    # Validate positive
    if value <= 0:
        logger.warning(
            f"[StartupTimeouts] Invalid value for {name}={value} "
            f"(must be positive), using default: {default}"
        )
        return default

    # Validate min
    if min_value is not None and value < min_value:
        logger.warning(
            f"[StartupTimeouts] Invalid value for {name}={value} "
            f"(below minimum {min_value}), using default: {default}"
        )
        return default

    # Validate max
    if max_value is not None and value > max_value:
        logger.warning(
            f"[StartupTimeouts] Invalid value for {name}={value} "
            f"(above maximum {max_value}), using default: {default}"
        )
        return default

    return value


# =============================================================================
# DEFAULT VALUES
# =============================================================================


# Maximum timeout - safety cap for all operations
# Must be >= largest default timeout (prime_startup_timeout = 600.0)
_DEFAULT_MAX_TIMEOUT = 900.0

# Signal timeouts (shutdown)
_DEFAULT_CLEANUP_TIMEOUT_SIGINT = 10.0
_DEFAULT_CLEANUP_TIMEOUT_SIGTERM = 5.0
_DEFAULT_CLEANUP_TIMEOUT_SIGKILL = 2.0

# Port/Network timeouts
_DEFAULT_PORT_CHECK_TIMEOUT = 1.0
_DEFAULT_PORT_RELEASE_WAIT = 2.0
_DEFAULT_IPC_SOCKET_TIMEOUT = 8.0

# Tool timeouts
_DEFAULT_LSOF_TIMEOUT = 5.0
_DEFAULT_DOCKER_CHECK_TIMEOUT = 10.0

# Health check timeouts
_DEFAULT_BACKEND_HEALTH_TIMEOUT = 30.0
_DEFAULT_FRONTEND_HEALTH_TIMEOUT = 60.0
_DEFAULT_LOADING_SERVER_HEALTH_TIMEOUT = 5.0

# Heartbeat
_DEFAULT_HEARTBEAT_INTERVAL = 5.0

# Trinity components
_DEFAULT_PRIME_STARTUP_TIMEOUT = 600.0  # 10 min for model loading
_DEFAULT_REACTOR_STARTUP_TIMEOUT = 120.0
_DEFAULT_REACTOR_HEALTH_TIMEOUT = 10.0

# Lock timeouts
_DEFAULT_STARTUP_LOCK_TIMEOUT = 30.0
_DEFAULT_TAKEOVER_HANDOVER_TIMEOUT = 15.0
_DEFAULT_MAX_LOCK_TIMEOUT = 300.0
_DEFAULT_MIN_LOCK_TIMEOUT = 0.1
_DEFAULT_DEFAULT_LOCK_TIMEOUT = 5.0
_DEFAULT_STALE_LOCK_RETRY_TIMEOUT = 1.0

# Broadcast timeout
_DEFAULT_BROADCAST_TIMEOUT = 2.0

# Async utility timeouts
_DEFAULT_PROCESS_WAIT_TIMEOUT = 10.0
_DEFAULT_SUBPROCESS_TIMEOUT = 30.0


# =============================================================================
# STARTUP TIMEOUTS CONFIGURATION CLASS
# =============================================================================


@dataclass
class StartupTimeouts:
    """
    Centralized timeout configuration for JARVIS startup/shutdown.

    All timeouts are loaded from environment variables at instantiation time.
    Invalid values trigger a warning log and fall back to defaults.

    Example:
        timeouts = StartupTimeouts()
        await asyncio.wait_for(operation(), timeout=timeouts.backend_health_timeout)
    """

    # -------------------------------------------------------------------------
    # Global Configuration
    # -------------------------------------------------------------------------

    max_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_MAX_TIMEOUT", _DEFAULT_MAX_TIMEOUT, min_value=1.0
    ))
    """Maximum allowed timeout for any operation. Safety cap to prevent unbounded waits."""

    # -------------------------------------------------------------------------
    # Signal Timeouts (Shutdown)
    # -------------------------------------------------------------------------

    cleanup_timeout_sigint: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_CLEANUP_TIMEOUT_SIGINT", _DEFAULT_CLEANUP_TIMEOUT_SIGINT, min_value=0.1
    ))
    """Wait time after sending SIGINT before escalating to SIGTERM."""

    cleanup_timeout_sigterm: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_CLEANUP_TIMEOUT_SIGTERM", _DEFAULT_CLEANUP_TIMEOUT_SIGTERM, min_value=0.1
    ))
    """Wait time after sending SIGTERM before escalating to SIGKILL."""

    cleanup_timeout_sigkill: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_CLEANUP_TIMEOUT_SIGKILL", _DEFAULT_CLEANUP_TIMEOUT_SIGKILL, min_value=0.1
    ))
    """Wait time after sending SIGKILL before giving up on process termination."""

    # -------------------------------------------------------------------------
    # Port and Network Timeouts
    # -------------------------------------------------------------------------

    port_check_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_PORT_CHECK_TIMEOUT", _DEFAULT_PORT_CHECK_TIMEOUT, min_value=0.1
    ))
    """Timeout for TCP port availability check (connect_ex)."""

    port_release_wait: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_PORT_RELEASE_WAIT", _DEFAULT_PORT_RELEASE_WAIT, min_value=0.1
    ))
    """Time to wait for port to be released after process exit."""

    ipc_socket_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_IPC_SOCKET_TIMEOUT", _DEFAULT_IPC_SOCKET_TIMEOUT, min_value=0.5
    ))
    """Timeout for Unix socket connections (supervisor IPC)."""

    # -------------------------------------------------------------------------
    # Tool Timeouts
    # -------------------------------------------------------------------------

    lsof_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_LSOF_TIMEOUT", _DEFAULT_LSOF_TIMEOUT, min_value=0.5
    ))
    """Timeout for lsof subprocess calls to check port usage."""

    docker_check_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_DOCKER_CHECK_TIMEOUT", _DEFAULT_DOCKER_CHECK_TIMEOUT, min_value=1.0
    ))
    """Timeout for docker daemon health checks."""

    # -------------------------------------------------------------------------
    # Health Check Timeouts
    # -------------------------------------------------------------------------

    backend_health_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_BACKEND_HEALTH_TIMEOUT", _DEFAULT_BACKEND_HEALTH_TIMEOUT, min_value=1.0
    ))
    """Timeout for backend HTTP health check endpoint."""

    frontend_health_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_FRONTEND_HEALTH_TIMEOUT", _DEFAULT_FRONTEND_HEALTH_TIMEOUT, min_value=1.0
    ))
    """Timeout for frontend health check (webpack dev server)."""

    loading_server_health_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_LOADING_SERVER_HEALTH_TIMEOUT", _DEFAULT_LOADING_SERVER_HEALTH_TIMEOUT, min_value=0.5
    ))
    """Timeout for loading server health check."""

    # -------------------------------------------------------------------------
    # Heartbeat Configuration
    # -------------------------------------------------------------------------

    heartbeat_interval: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_HEARTBEAT_INTERVAL", _DEFAULT_HEARTBEAT_INTERVAL, min_value=1.0
    ))
    """Interval between heartbeat broadcasts during startup."""

    # -------------------------------------------------------------------------
    # Trinity Component Timeouts
    # -------------------------------------------------------------------------

    prime_startup_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_PRIME_STARTUP_TIMEOUT", _DEFAULT_PRIME_STARTUP_TIMEOUT, min_value=10.0
    ))
    """Timeout for JARVIS-Prime startup (includes model loading)."""

    reactor_startup_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_REACTOR_STARTUP_TIMEOUT", _DEFAULT_REACTOR_STARTUP_TIMEOUT, min_value=5.0
    ))
    """Timeout for Reactor-Core startup."""

    reactor_health_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_REACTOR_HEALTH_TIMEOUT", _DEFAULT_REACTOR_HEALTH_TIMEOUT, min_value=1.0
    ))
    """Timeout for Reactor-Core health check."""

    # -------------------------------------------------------------------------
    # Lock Timeouts
    # -------------------------------------------------------------------------

    startup_lock_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_STARTUP_LOCK_TIMEOUT", _DEFAULT_STARTUP_LOCK_TIMEOUT, min_value=1.0
    ))
    """Timeout for acquiring the startup lock (single-instance coordination)."""

    takeover_handover_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_TAKEOVER_HANDOVER_TIMEOUT", _DEFAULT_TAKEOVER_HANDOVER_TIMEOUT, min_value=1.0
    ))
    """Timeout for graceful handover during instance takeover."""

    max_lock_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_MAX_LOCK_TIMEOUT", _DEFAULT_MAX_LOCK_TIMEOUT, min_value=1.0
    ))
    """Maximum allowed lock acquisition timeout."""

    min_lock_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_MIN_LOCK_TIMEOUT", _DEFAULT_MIN_LOCK_TIMEOUT, min_value=0.01
    ))
    """Minimum allowed lock acquisition timeout (prevents spin-lock)."""

    default_lock_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_DEFAULT_LOCK_TIMEOUT", _DEFAULT_DEFAULT_LOCK_TIMEOUT, min_value=0.1
    ))
    """Default lock acquisition timeout when not specified."""

    stale_lock_retry_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_STALE_LOCK_RETRY_TIMEOUT", _DEFAULT_STALE_LOCK_RETRY_TIMEOUT, min_value=0.1
    ))
    """Timeout for retry attempt after removing a stale lock."""

    # -------------------------------------------------------------------------
    # Broadcast Timeout
    # -------------------------------------------------------------------------

    broadcast_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_BROADCAST_TIMEOUT", _DEFAULT_BROADCAST_TIMEOUT, min_value=0.1
    ))
    """Timeout for progress/status broadcasts to clients."""

    # -------------------------------------------------------------------------
    # Async Utility Timeouts
    # -------------------------------------------------------------------------

    process_wait_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_PROCESS_WAIT_TIMEOUT", _DEFAULT_PROCESS_WAIT_TIMEOUT, min_value=0.1
    ))
    """Default timeout for async_process_wait operations."""

    subprocess_timeout: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_SUBPROCESS_TIMEOUT", _DEFAULT_SUBPROCESS_TIMEOUT, min_value=1.0
    ))
    """Default timeout for async_subprocess_run operations."""

    # -------------------------------------------------------------------------
    # Validation Methods
    # -------------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Validate timeout relationships after initialization."""
        # Validate all timeout fields are <= max_timeout (per spec requirement)
        # Get all fields except max_timeout itself
        timeout_fields = [
            "cleanup_timeout_sigint",
            "cleanup_timeout_sigterm",
            "cleanup_timeout_sigkill",
            "port_check_timeout",
            "port_release_wait",
            "ipc_socket_timeout",
            "lsof_timeout",
            "docker_check_timeout",
            "backend_health_timeout",
            "frontend_health_timeout",
            "loading_server_health_timeout",
            "heartbeat_interval",
            "prime_startup_timeout",
            "reactor_startup_timeout",
            "reactor_health_timeout",
            "startup_lock_timeout",
            "takeover_handover_timeout",
            "max_lock_timeout",
            "min_lock_timeout",
            "default_lock_timeout",
            "stale_lock_retry_timeout",
            "broadcast_timeout",
            "process_wait_timeout",
            "subprocess_timeout",
        ]

        for field_name in timeout_fields:
            value = getattr(self, field_name)
            if value > self.max_timeout:
                logger.warning(
                    f"[StartupTimeouts] {field_name}={value} exceeds max_timeout={self.max_timeout}, "
                    f"capping to max_timeout"
                )
                object.__setattr__(self, field_name, self.max_timeout)

        # Ensure lock timeout bounds are consistent
        if self.min_lock_timeout >= self.max_lock_timeout:
            logger.warning(
                f"[StartupTimeouts] min_lock_timeout ({self.min_lock_timeout}) >= "
                f"max_lock_timeout ({self.max_lock_timeout}), adjusting min to 0.1"
            )
            object.__setattr__(self, "min_lock_timeout", 0.1)

        # Ensure default_lock_timeout is within bounds
        if self.default_lock_timeout < self.min_lock_timeout:
            logger.warning(
                f"[StartupTimeouts] default_lock_timeout ({self.default_lock_timeout}) < "
                f"min_lock_timeout ({self.min_lock_timeout}), adjusting to min"
            )
            object.__setattr__(self, "default_lock_timeout", self.min_lock_timeout)

        if self.default_lock_timeout > self.max_lock_timeout:
            logger.warning(
                f"[StartupTimeouts] default_lock_timeout ({self.default_lock_timeout}) > "
                f"max_lock_timeout ({self.max_lock_timeout}), adjusting to max"
            )
            object.__setattr__(self, "default_lock_timeout", self.max_lock_timeout)

        # Log successful initialization at debug level
        logger.debug(
            f"[StartupTimeouts] Initialized with max_timeout={self.max_timeout}, "
            f"prime_startup_timeout={self.prime_startup_timeout}"
        )

    def validate_timeout(self, timeout: float, name: str = "timeout") -> float:
        """
        Validate a timeout value is within acceptable bounds.

        Args:
            timeout: Timeout value to validate
            name: Name of the timeout for error messages

        Returns:
            Validated timeout (clamped to bounds if out of range)

        Raises:
            ValueError: If timeout is <= 0
        """
        if timeout <= 0:
            raise ValueError(f"{name} must be positive, got {timeout}")

        if timeout > self.max_timeout:
            logger.warning(
                f"[StartupTimeouts] {name}={timeout} exceeds max_timeout={self.max_timeout}, "
                f"clamping to max"
            )
            return self.max_timeout

        return timeout

    def validate_lock_timeout(self, timeout: float) -> float:
        """
        Validate a lock-specific timeout value.

        Args:
            timeout: Lock timeout to validate

        Returns:
            Validated timeout (clamped to lock bounds if out of range)

        Raises:
            ValueError: If timeout is <= 0
        """
        if timeout <= 0:
            raise ValueError(f"Lock timeout must be positive, got {timeout}")

        if timeout < self.min_lock_timeout:
            logger.warning(
                f"[StartupTimeouts] lock timeout={timeout} below min={self.min_lock_timeout}, "
                f"clamping to min"
            )
            return self.min_lock_timeout

        if timeout > self.max_lock_timeout:
            logger.warning(
                f"[StartupTimeouts] lock timeout={timeout} exceeds max={self.max_lock_timeout}, "
                f"clamping to max"
            )
            return self.max_lock_timeout

        return timeout

    def get_signal_timeouts(self) -> tuple[float, float, float]:
        """
        Get all signal timeouts as a tuple.

        Returns:
            Tuple of (sigint_timeout, sigterm_timeout, sigkill_timeout)
        """
        return (
            self.cleanup_timeout_sigint,
            self.cleanup_timeout_sigterm,
            self.cleanup_timeout_sigkill,
        )

    def to_dict(self) -> dict:
        """
        Export all timeout values as a dictionary.

        Useful for logging or debugging configuration.

        Returns:
            Dictionary of all timeout values
        """
        return {
            # Global
            "max_timeout": self.max_timeout,
            # Signal
            "cleanup_timeout_sigint": self.cleanup_timeout_sigint,
            "cleanup_timeout_sigterm": self.cleanup_timeout_sigterm,
            "cleanup_timeout_sigkill": self.cleanup_timeout_sigkill,
            # Port/Network
            "port_check_timeout": self.port_check_timeout,
            "port_release_wait": self.port_release_wait,
            "ipc_socket_timeout": self.ipc_socket_timeout,
            # Tools
            "lsof_timeout": self.lsof_timeout,
            "docker_check_timeout": self.docker_check_timeout,
            # Health
            "backend_health_timeout": self.backend_health_timeout,
            "frontend_health_timeout": self.frontend_health_timeout,
            "loading_server_health_timeout": self.loading_server_health_timeout,
            # Heartbeat
            "heartbeat_interval": self.heartbeat_interval,
            # Trinity
            "prime_startup_timeout": self.prime_startup_timeout,
            "reactor_startup_timeout": self.reactor_startup_timeout,
            "reactor_health_timeout": self.reactor_health_timeout,
            # Locks
            "startup_lock_timeout": self.startup_lock_timeout,
            "takeover_handover_timeout": self.takeover_handover_timeout,
            "max_lock_timeout": self.max_lock_timeout,
            "min_lock_timeout": self.min_lock_timeout,
            "default_lock_timeout": self.default_lock_timeout,
            "stale_lock_retry_timeout": self.stale_lock_retry_timeout,
            # Broadcast
            "broadcast_timeout": self.broadcast_timeout,
            # Async utilities
            "process_wait_timeout": self.process_wait_timeout,
            "subprocess_timeout": self.subprocess_timeout,
        }


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================


# Lazily-initialized singleton instance
_timeouts_instance: Optional[StartupTimeouts] = None


def get_timeouts() -> StartupTimeouts:
    """
    Get the module-level StartupTimeouts singleton.

    This provides a convenient way to access timeouts without
    creating new instances, while still being lazy-initialized.

    Returns:
        StartupTimeouts singleton instance
    """
    global _timeouts_instance
    if _timeouts_instance is None:
        _timeouts_instance = StartupTimeouts()
    return _timeouts_instance


def reset_timeouts() -> None:
    """
    Reset the module-level singleton (primarily for testing).

    This forces the next get_timeouts() call to create a fresh instance,
    picking up any environment variable changes.
    """
    global _timeouts_instance
    _timeouts_instance = None


# =============================================================================
# PHASE BUDGET DEFAULTS
# =============================================================================


_DEFAULT_PRE_TRINITY_BUDGET = 30.0
_DEFAULT_TRINITY_PHASE_BUDGET = 300.0
_DEFAULT_GCP_WAIT_BUFFER = 120.0
_DEFAULT_POST_TRINITY_BUDGET = 60.0
_DEFAULT_DISCOVERY_BUDGET = 45.0
_DEFAULT_HEALTH_CHECK_BUDGET = 30.0
_DEFAULT_CLEANUP_BUDGET = 30.0
_DEFAULT_SAFETY_MARGIN = 30.0
_DEFAULT_STARTUP_HARD_CAP = 900.0


# =============================================================================
# STARTUP METRICS HISTORY PROTOCOL
# =============================================================================


class StartupMetricsHistory:
    """
    Protocol/interface for startup metrics history.

    This is an optional dependency for StartupTimeoutCalculator that provides
    historical timing data for adaptive timeout calculations.

    Implementations should track p95 (95th percentile) timings for each
    startup phase to enable data-driven timeout adjustments.
    """

    def has(self, phase: str) -> bool:
        """
        Check whether history exists for a given phase.

        Args:
            phase: Phase name (e.g., "TRINITY_PHASE", "PRE_TRINITY")

        Returns:
            True if historical data exists for this phase
        """
        raise NotImplementedError

    def get_p95(self, phase: str) -> Optional[float]:
        """
        Get the 95th percentile timing for a phase.

        Args:
            phase: Phase name (e.g., "TRINITY_PHASE", "PRE_TRINITY")

        Returns:
            P95 timing in seconds, or None if no data
        """
        raise NotImplementedError


# =============================================================================
# PHASE BUDGETS DATACLASS
# =============================================================================


@dataclass
class PhaseBudgets:
    """
    Startup phase budget configuration.

    Each phase has a default budget (in seconds) that can be overridden
    via environment variables with JARVIS_ prefix.

    The SAFETY_MARGIN is NOT included in the phase budgets dict - it's
    added separately to the global_timeout calculation.

    The HARD_CAP represents the absolute maximum timeout regardless of
    calculated values.

    Environment Variables:
    - JARVIS_PRE_TRINITY_BUDGET: Pre-Trinity initialization (default: 30s)
    - JARVIS_TRINITY_PHASE_BUDGET: Trinity component startup (default: 300s)
    - JARVIS_GCP_WAIT_BUFFER: GCP credential acquisition (default: 120s)
    - JARVIS_POST_TRINITY_BUDGET: Post-Trinity setup (default: 60s)
    - JARVIS_DISCOVERY_BUDGET: Service discovery (default: 45s)
    - JARVIS_HEALTH_CHECK_BUDGET: Health checks (default: 30s)
    - JARVIS_CLEANUP_BUDGET: Cleanup operations (default: 30s)
    - JARVIS_SAFETY_MARGIN: Safety buffer (default: 30s)
    - JARVIS_STARTUP_HARD_CAP: Absolute maximum timeout (default: 900s)
    """

    PRE_TRINITY: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_PRE_TRINITY_BUDGET", _DEFAULT_PRE_TRINITY_BUDGET, min_value=1.0
    ))
    """Budget for pre-Trinity initialization phase."""

    TRINITY_PHASE: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_TRINITY_PHASE_BUDGET", _DEFAULT_TRINITY_PHASE_BUDGET, min_value=10.0
    ))
    """Budget for Trinity component startup (JARVIS-Prime, Reactor-Core)."""

    GCP_WAIT_BUFFER: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_GCP_WAIT_BUFFER", _DEFAULT_GCP_WAIT_BUFFER, min_value=10.0
    ))
    """Buffer for GCP credential acquisition and cloud service initialization."""

    POST_TRINITY: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_POST_TRINITY_BUDGET", _DEFAULT_POST_TRINITY_BUDGET, min_value=5.0
    ))
    """Budget for post-Trinity setup phase."""

    DISCOVERY: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_DISCOVERY_BUDGET", _DEFAULT_DISCOVERY_BUDGET, min_value=5.0
    ))
    """Budget for service discovery phase."""

    HEALTH_CHECK: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_HEALTH_CHECK_BUDGET", _DEFAULT_HEALTH_CHECK_BUDGET, min_value=5.0
    ))
    """Budget for health check verification phase."""

    CLEANUP: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_CLEANUP_BUDGET", _DEFAULT_CLEANUP_BUDGET, min_value=5.0
    ))
    """Budget for cleanup operations during startup."""

    SAFETY_MARGIN: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_SAFETY_MARGIN", _DEFAULT_SAFETY_MARGIN, min_value=5.0
    ))
    """Safety buffer added to global_timeout (not in phase budgets)."""

    HARD_CAP: float = field(default_factory=lambda: _get_env_float(
        "JARVIS_STARTUP_HARD_CAP", _DEFAULT_STARTUP_HARD_CAP, min_value=60.0
    ))
    """Absolute maximum timeout cap for any operation."""

    def get_phase_budget(self, phase: str) -> float:
        """
        Get the budget for a specific phase.

        Args:
            phase: Phase name (e.g., "PRE_TRINITY", "TRINITY_PHASE")

        Returns:
            Budget in seconds

        Raises:
            KeyError: If phase name is not recognized
        """
        budgets = {
            "PRE_TRINITY": self.PRE_TRINITY,
            "TRINITY_PHASE": self.TRINITY_PHASE,
            "GCP_WAIT_BUFFER": self.GCP_WAIT_BUFFER,
            "POST_TRINITY": self.POST_TRINITY,
            "DISCOVERY": self.DISCOVERY,
            "HEALTH_CHECK": self.HEALTH_CHECK,
            "CLEANUP": self.CLEANUP,
        }
        if phase not in budgets:
            raise KeyError(f"Unknown phase: {phase}")
        return budgets[phase]


# =============================================================================
# STARTUP TIMEOUT CALCULATOR
# =============================================================================


class StartupTimeoutCalculator:
    """
    Calculator for startup phase timeouts with adaptive history support.

    This calculator replaces the arbitrary 900s global timeout with
    bottom-up per-phase budgets. It supports:

    1. Static phase budgets (from PhaseBudgets dataclass)
    2. Adaptive timeouts based on historical p95 metrics
    3. Conditional inclusion of Trinity and GCP phases
    4. Hard cap enforcement for safety

    The calculator uses the formula for effective timeout:
    - With history: min(max(base, p95 * 1.2), HARD_CAP)
    - Without history: min(base, HARD_CAP)

    Example:
        calculator = StartupTimeoutCalculator(trinity_enabled=True, gcp_enabled=True)
        global_timeout = calculator.global_timeout  # Sum of all phase budgets
        trinity_budget = calculator.trinity_budget  # Just Trinity + GCP phases
    """

    def __init__(
        self,
        trinity_enabled: bool = True,
        gcp_enabled: bool = False,
        history: Optional[StartupMetricsHistory] = None,
    ) -> None:
        """
        Initialize the timeout calculator.

        Args:
            trinity_enabled: Whether Trinity components are enabled
            gcp_enabled: Whether GCP cloud services are enabled
            history: Optional metrics history for adaptive timeouts
        """
        self._trinity_enabled = trinity_enabled
        self._gcp_enabled = gcp_enabled
        self._history = history
        self._budgets = PhaseBudgets()

    def effective(self, phase: str) -> float:
        """
        Calculate effective timeout for a phase.

        If history exists and has data for this phase:
            effective = min(max(base, p95 * 1.2), HARD_CAP)

        Otherwise (no history):
            effective = min(base, HARD_CAP)

        Args:
            phase: Phase name (e.g., "TRINITY_PHASE", "PRE_TRINITY")

        Returns:
            Effective timeout in seconds

        Raises:
            KeyError: If phase name is not recognized
        """
        base = self._budgets.get_phase_budget(phase)

        if self._history is not None and self._history.has(phase):
            p95 = self._history.get_p95(phase)
            if p95 is not None:
                adaptive = p95 * 1.2
                return min(max(base, adaptive), self._budgets.HARD_CAP)

        return min(base, self._budgets.HARD_CAP)

    @property
    def trinity_budget(self) -> float:
        """
        Calculate Trinity phase budget.

        If trinity_enabled:
            effective("TRINITY_PHASE") + (effective("GCP_WAIT_BUFFER") if gcp_enabled else 0)
        Else:
            0.0

        Returns:
            Trinity budget in seconds
        """
        if not self._trinity_enabled:
            return 0.0

        budget = self.effective("TRINITY_PHASE")
        if self._gcp_enabled:
            budget += self.effective("GCP_WAIT_BUFFER")

        return budget

    @property
    def global_timeout(self) -> float:
        """
        Calculate global startup timeout.

        Sum of effective() for all included phases + SAFETY_MARGIN.

        Excluded phases when disabled:
        - If NOT trinity_enabled: exclude TRINITY_PHASE and GCP_WAIT_BUFFER
        - If NOT gcp_enabled: exclude GCP_WAIT_BUFFER

        Returns:
            Global timeout in seconds
        """
        # Always-included phases
        phases = ["PRE_TRINITY", "POST_TRINITY", "DISCOVERY", "HEALTH_CHECK", "CLEANUP"]

        # Conditionally include Trinity phases
        if self._trinity_enabled:
            phases.append("TRINITY_PHASE")
            if self._gcp_enabled:
                phases.append("GCP_WAIT_BUFFER")

        # Sum all effective phase timeouts
        total = sum(self.effective(phase) for phase in phases)

        # Add safety margin (not capped by effective(), just added directly)
        total += self._budgets.SAFETY_MARGIN

        return total


# =============================================================================
# EXPORTS
# =============================================================================


__all__ = [
    # Main classes
    "StartupTimeouts",
    "PhaseBudgets",
    "StartupTimeoutCalculator",
    "StartupMetricsHistory",
    # Singleton access
    "get_timeouts",
    "reset_timeouts",
    # Validation utilities (for testing)
    "_get_env_float",
    # Default values (for reference/testing)
    "_DEFAULT_MAX_TIMEOUT",
    "_DEFAULT_CLEANUP_TIMEOUT_SIGINT",
    "_DEFAULT_CLEANUP_TIMEOUT_SIGTERM",
    "_DEFAULT_CLEANUP_TIMEOUT_SIGKILL",
    "_DEFAULT_PORT_CHECK_TIMEOUT",
    "_DEFAULT_PORT_RELEASE_WAIT",
    "_DEFAULT_IPC_SOCKET_TIMEOUT",
    "_DEFAULT_LSOF_TIMEOUT",
    "_DEFAULT_DOCKER_CHECK_TIMEOUT",
    "_DEFAULT_BACKEND_HEALTH_TIMEOUT",
    "_DEFAULT_FRONTEND_HEALTH_TIMEOUT",
    "_DEFAULT_LOADING_SERVER_HEALTH_TIMEOUT",
    "_DEFAULT_HEARTBEAT_INTERVAL",
    "_DEFAULT_PRIME_STARTUP_TIMEOUT",
    "_DEFAULT_REACTOR_STARTUP_TIMEOUT",
    "_DEFAULT_REACTOR_HEALTH_TIMEOUT",
    "_DEFAULT_STARTUP_LOCK_TIMEOUT",
    "_DEFAULT_TAKEOVER_HANDOVER_TIMEOUT",
    "_DEFAULT_MAX_LOCK_TIMEOUT",
    "_DEFAULT_MIN_LOCK_TIMEOUT",
    "_DEFAULT_DEFAULT_LOCK_TIMEOUT",
    "_DEFAULT_STALE_LOCK_RETRY_TIMEOUT",
    "_DEFAULT_BROADCAST_TIMEOUT",
    "_DEFAULT_PROCESS_WAIT_TIMEOUT",
    "_DEFAULT_SUBPROCESS_TIMEOUT",
    # Phase budget defaults
    "_DEFAULT_PRE_TRINITY_BUDGET",
    "_DEFAULT_TRINITY_PHASE_BUDGET",
    "_DEFAULT_GCP_WAIT_BUFFER",
    "_DEFAULT_POST_TRINITY_BUDGET",
    "_DEFAULT_DISCOVERY_BUDGET",
    "_DEFAULT_HEALTH_CHECK_BUDGET",
    "_DEFAULT_CLEANUP_BUDGET",
    "_DEFAULT_SAFETY_MARGIN",
    "_DEFAULT_STARTUP_HARD_CAP",
]

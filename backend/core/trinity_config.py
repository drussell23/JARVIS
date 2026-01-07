"""
Trinity Unified Configuration System v79.0
==========================================

ZERO-HARDCODING: All configuration values are sourced from environment variables
with intelligent defaults. This module is shared across all three Trinity repos:
- JARVIS (Body)
- JARVIS-Prime (Mind)
- Reactor-Core (Nerves)

FEATURES:
    - Environment variable overrides for all settings
    - Type-safe configuration with validation
    - Intelligent defaults based on system detection
    - Dynamic reconfiguration without restart
    - Unified configuration across all repos
    - Service discovery support

USAGE:
    from backend.core.trinity_config import get_config, TrinityConfig

    config = get_config()
    print(config.trinity_dir)  # ~/.jarvis/trinity
    print(config.heartbeat_interval)  # 5.0 (configurable via TRINITY_HEARTBEAT_INTERVAL)

ENVIRONMENT VARIABLES:
    TRINITY_DIR                     - Base directory for Trinity files
    TRINITY_HEARTBEAT_INTERVAL      - Heartbeat interval in seconds
    TRINITY_HEARTBEAT_TIMEOUT       - Heartbeat timeout in seconds
    TRINITY_HEALTH_CHECK_INTERVAL   - Health check interval in seconds
    TRINITY_CIRCUIT_BREAKER_THRESHOLD - Failures before circuit opens
    TRINITY_CIRCUIT_BREAKER_RESET   - Seconds before circuit resets
    TRINITY_COMMAND_TIMEOUT         - Command timeout in seconds
    TRINITY_MAX_RETRIES             - Maximum retry attempts
    TRINITY_RETRY_DELAY             - Initial retry delay in seconds
    TRINITY_RETRY_MAX_DELAY         - Maximum retry delay (with backoff)
    TRINITY_STALE_THRESHOLD         - Seconds before state is stale
    TRINITY_DLQ_RETRY_INTERVAL      - Dead letter queue retry interval
    TRINITY_DLQ_MAX_RETRIES         - DLQ max retry attempts
    TRINITY_JITTER_FACTOR           - Jitter factor for polling (0.0-1.0)

    JARVIS_HOST                     - JARVIS API host
    JARVIS_PORT                     - JARVIS API port
    JARVIS_PRIME_HOST               - JARVIS Prime API host
    JARVIS_PRIME_PORT               - JARVIS Prime API port
    REACTOR_CORE_HOST               - Reactor Core API host
    REACTOR_CORE_PORT               - Reactor Core API port

    TRINITY_FILE_OPERATION_TIMEOUT  - File operation timeout
    TRINITY_MODEL_LOAD_TIMEOUT      - Model loading timeout
    TRINITY_BRIDGE_INIT_TIMEOUT     - Bridge initialization timeout
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Union

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# ENVIRONMENT VARIABLE HELPERS
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
        logger.warning(f"[TrinityConfig] Invalid int for {key}: {value}, using default: {default}")
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[TrinityConfig] Invalid float for {key}: {value}, using default: {default}")
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


def _env_list(key: str, default: List[str], separator: str = ",") -> List[str]:
    """Get list from environment."""
    value = os.getenv(key)
    if value is None:
        return default
    return [item.strip() for item in value.split(separator) if item.strip()]


# =============================================================================
# CONFIGURATION ENUMS
# =============================================================================


class ComponentType(Enum):
    """Types of Trinity components."""
    JARVIS_BODY = "jarvis_body"
    JARVIS_PRIME = "jarvis_prime"
    REACTOR_CORE = "reactor_core"


class ComponentHealth(Enum):
    """Health states for components."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"      # Normal operation
    OPEN = "open"          # Blocking requests
    HALF_OPEN = "half_open"  # Testing recovery


# =============================================================================
# CONFIGURATION DATACLASSES
# =============================================================================


@dataclass
class ServiceEndpoint:
    """Configuration for a service endpoint."""
    host: str
    port: int
    health_path: str = "/health"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}{self.health_path}"

    def __str__(self) -> str:
        return self.base_url


@dataclass
class TimeoutConfig:
    """Timeout configuration for various operations."""
    file_operation: float = field(default_factory=lambda: _env_float("TRINITY_FILE_OPERATION_TIMEOUT", 10.0))
    model_load: float = field(default_factory=lambda: _env_float("TRINITY_MODEL_LOAD_TIMEOUT", 300.0))
    bridge_init: float = field(default_factory=lambda: _env_float("TRINITY_BRIDGE_INIT_TIMEOUT", 30.0))
    command_execution: float = field(default_factory=lambda: _env_float("TRINITY_COMMAND_TIMEOUT", 30.0))
    health_check: float = field(default_factory=lambda: _env_float("TRINITY_HEALTH_CHECK_TIMEOUT", 5.0))
    stream_token: float = field(default_factory=lambda: _env_float("TRINITY_STREAM_TOKEN_TIMEOUT", 30.0))
    stream_total: float = field(default_factory=lambda: _env_float("TRINITY_STREAM_TOTAL_TIMEOUT", 300.0))
    port_release: float = field(default_factory=lambda: _env_float("TRINITY_PORT_RELEASE_TIMEOUT", 10.0))
    graceful_shutdown: float = field(default_factory=lambda: _env_float("TRINITY_GRACEFUL_SHUTDOWN_TIMEOUT", 30.0))


@dataclass
class RetryConfig:
    """Retry configuration with exponential backoff."""
    max_retries: int = field(default_factory=lambda: _env_int("TRINITY_MAX_RETRIES", 3))
    initial_delay: float = field(default_factory=lambda: _env_float("TRINITY_RETRY_DELAY", 0.1))
    max_delay: float = field(default_factory=lambda: _env_float("TRINITY_RETRY_MAX_DELAY", 30.0))
    exponential_base: float = field(default_factory=lambda: _env_float("TRINITY_RETRY_EXPONENTIAL_BASE", 2.0))
    jitter_factor: float = field(default_factory=lambda: _env_float("TRINITY_JITTER_FACTOR", 0.1))

    def get_delay(self, attempt: int) -> float:
        """
        Calculate delay for retry attempt with exponential backoff and jitter.

        Args:
            attempt: The retry attempt number (0-indexed)

        Returns:
            Delay in seconds with jitter applied
        """
        # Exponential backoff
        delay = self.initial_delay * (self.exponential_base ** attempt)

        # Cap at max delay
        delay = min(delay, self.max_delay)

        # Add jitter to prevent thundering herd
        jitter = delay * self.jitter_factor * random.random()

        return delay + jitter


@dataclass
class CircuitBreakerConfig:
    """Circuit breaker configuration."""
    failure_threshold: int = field(default_factory=lambda: _env_int("TRINITY_CIRCUIT_BREAKER_THRESHOLD", 5))
    success_threshold: int = field(default_factory=lambda: _env_int("TRINITY_CIRCUIT_BREAKER_SUCCESS_THRESHOLD", 3))
    timeout_seconds: float = field(default_factory=lambda: _env_float("TRINITY_CIRCUIT_BREAKER_RESET", 30.0))
    half_open_max_requests: int = field(default_factory=lambda: _env_int("TRINITY_CIRCUIT_BREAKER_HALF_OPEN_MAX", 3))


@dataclass
class DeadLetterQueueConfig:
    """Dead Letter Queue configuration."""
    enabled: bool = field(default_factory=lambda: _env_bool("TRINITY_DLQ_ENABLED", True))
    retry_interval: float = field(default_factory=lambda: _env_float("TRINITY_DLQ_RETRY_INTERVAL", 60.0))
    max_retries: int = field(default_factory=lambda: _env_int("TRINITY_DLQ_MAX_RETRIES", 3))
    persist_to_disk: bool = field(default_factory=lambda: _env_bool("TRINITY_DLQ_PERSIST", True))
    max_age_hours: float = field(default_factory=lambda: _env_float("TRINITY_DLQ_MAX_AGE_HOURS", 24.0))


@dataclass
class HealthMonitorConfig:
    """Health monitoring configuration."""
    heartbeat_interval: float = field(default_factory=lambda: _env_float("TRINITY_HEARTBEAT_INTERVAL", 5.0))
    heartbeat_timeout: float = field(default_factory=lambda: _env_float("TRINITY_HEARTBEAT_TIMEOUT", 15.0))
    health_check_interval: float = field(default_factory=lambda: _env_float("TRINITY_HEALTH_CHECK_INTERVAL", 5.0))
    stale_threshold: float = field(default_factory=lambda: _env_float("TRINITY_STALE_THRESHOLD", 120.0))
    deduplication_window: float = field(default_factory=lambda: _env_float("TRINITY_DEDUP_WINDOW", 60.0))


# =============================================================================
# MAIN CONFIGURATION CLASS
# =============================================================================


@dataclass
class TrinityConfig:
    """
    Unified Trinity Configuration - v79.0

    All values are sourced from environment variables with intelligent defaults.
    No hardcoding - everything is configurable.
    """

    # Base directories
    trinity_dir: Path = field(default_factory=lambda: _env_path("TRINITY_DIR", Path.home() / ".jarvis" / "trinity"))

    # Feature flags
    enabled: bool = field(default_factory=lambda: _env_bool("TRINITY_ENABLED", True))
    debug_mode: bool = field(default_factory=lambda: _env_bool("TRINITY_DEBUG", False))

    # Service endpoints
    jarvis_endpoint: ServiceEndpoint = field(default_factory=lambda: ServiceEndpoint(
        host=_env_str("JARVIS_HOST", "localhost"),
        port=_env_int("JARVIS_PORT", 8010),
        health_path=_env_str("JARVIS_HEALTH_PATH", "/health/ping"),
    ))

    jarvis_prime_endpoint: ServiceEndpoint = field(default_factory=lambda: ServiceEndpoint(
        host=_env_str("JARVIS_PRIME_HOST", "localhost"),
        port=_env_int("JARVIS_PRIME_PORT", 8002),
        health_path=_env_str("JARVIS_PRIME_HEALTH_PATH", "/health"),
    ))

    reactor_core_endpoint: ServiceEndpoint = field(default_factory=lambda: ServiceEndpoint(
        host=_env_str("REACTOR_CORE_HOST", "localhost"),
        port=_env_int("REACTOR_CORE_PORT", 8003),
        health_path=_env_str("REACTOR_CORE_HEALTH_PATH", "/health"),
    ))

    # Sub-configurations
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    dlq: DeadLetterQueueConfig = field(default_factory=DeadLetterQueueConfig)
    health: HealthMonitorConfig = field(default_factory=HealthMonitorConfig)

    # Process patterns for cleanup (configurable)
    process_patterns: List[str] = field(default_factory=lambda: _env_list(
        "JARVIS_PROCESS_PATTERNS",
        ["start_system.py", "main.py", "jarvis", "run_supervisor.py"]
    ))

    # PID file locations (configurable)
    pid_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_PID_DIR",
        Path(os.getenv("XDG_RUNTIME_DIR", "/tmp"))
    ))

    def __post_init__(self):
        """Ensure directories exist after initialization."""
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        directories = [
            self.trinity_dir,
            self.trinity_dir / "commands",
            self.trinity_dir / "heartbeats",
            self.trinity_dir / "components",
            self.trinity_dir / "responses",
            self.trinity_dir / "dlq",
            self.pid_dir,
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    # Convenience properties for directory paths
    @property
    def commands_dir(self) -> Path:
        return self.trinity_dir / "commands"

    @property
    def heartbeats_dir(self) -> Path:
        return self.trinity_dir / "heartbeats"

    @property
    def components_dir(self) -> Path:
        return self.trinity_dir / "components"

    @property
    def responses_dir(self) -> Path:
        return self.trinity_dir / "responses"

    @property
    def dlq_dir(self) -> Path:
        return self.trinity_dir / "dlq"

    @property
    def state_file(self) -> Path:
        return self.trinity_dir / "orchestrator_state.json"

    def get_endpoint(self, component: ComponentType) -> ServiceEndpoint:
        """Get endpoint configuration for a component."""
        endpoints = {
            ComponentType.JARVIS_BODY: self.jarvis_endpoint,
            ComponentType.JARVIS_PRIME: self.jarvis_prime_endpoint,
            ComponentType.REACTOR_CORE: self.reactor_core_endpoint,
        }
        return endpoints[component]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize configuration to dictionary."""
        return {
            "trinity_dir": str(self.trinity_dir),
            "enabled": self.enabled,
            "debug_mode": self.debug_mode,
            "jarvis": str(self.jarvis_endpoint),
            "jarvis_prime": str(self.jarvis_prime_endpoint),
            "reactor_core": str(self.reactor_core_endpoint),
            "timeouts": {
                "file_operation": self.timeouts.file_operation,
                "model_load": self.timeouts.model_load,
                "command_execution": self.timeouts.command_execution,
            },
            "retry": {
                "max_retries": self.retry.max_retries,
                "initial_delay": self.retry.initial_delay,
                "max_delay": self.retry.max_delay,
            },
            "health": {
                "heartbeat_interval": self.health.heartbeat_interval,
                "heartbeat_timeout": self.health.heartbeat_timeout,
            },
        }


# =============================================================================
# SINGLETON PATTERN - Thread-Safe with Double-Check Locking
# =============================================================================

_config: Optional[TrinityConfig] = None
_config_lock = threading.Lock()


def get_config() -> TrinityConfig:
    """
    Get the global Trinity configuration singleton.

    Uses double-check locking for thread safety.

    Returns:
        TrinityConfig instance
    """
    global _config

    # Fast path: Already initialized
    if _config is not None:
        return _config

    # Slow path: Acquire lock and double-check
    with _config_lock:
        if _config is None:
            _config = TrinityConfig()
            logger.info(f"[TrinityConfig] Initialized with trinity_dir={_config.trinity_dir}")
        return _config


def reload_config() -> TrinityConfig:
    """
    Reload configuration from environment variables.

    Use this after changing environment variables to pick up new values.

    Returns:
        New TrinityConfig instance
    """
    global _config

    with _config_lock:
        _config = TrinityConfig()
        logger.info("[TrinityConfig] Configuration reloaded")
        return _config


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def add_jitter(delay: float, jitter_factor: Optional[float] = None) -> float:
    """
    Add jitter to a delay value to prevent thundering herd.

    Args:
        delay: Base delay in seconds
        jitter_factor: Jitter factor (0.0-1.0), defaults to config value

    Returns:
        Delay with jitter applied
    """
    if jitter_factor is None:
        jitter_factor = get_config().retry.jitter_factor

    jitter = delay * jitter_factor * random.random()
    return delay + jitter


async def sleep_with_jitter(delay: float, jitter_factor: Optional[float] = None) -> None:
    """
    Async sleep with jitter to prevent thundering herd.

    Args:
        delay: Base delay in seconds
        jitter_factor: Jitter factor (0.0-1.0), defaults to config value
    """
    actual_delay = add_jitter(delay, jitter_factor)
    await asyncio.sleep(actual_delay)


def get_retry_delay(attempt: int, config: Optional[RetryConfig] = None) -> float:
    """
    Get retry delay for a given attempt number.

    Args:
        attempt: Retry attempt number (0-indexed)
        config: Optional retry config, uses global if not provided

    Returns:
        Delay in seconds with exponential backoff and jitter
    """
    if config is None:
        config = get_config().retry
    return config.get_delay(attempt)


# =============================================================================
# VALIDATION HELPERS
# =============================================================================


def validate_port(port: int, name: str = "port") -> int:
    """Validate port number."""
    if not (1 <= port <= 65535):
        raise ValueError(f"Invalid {name}: {port}. Must be 1-65535.")
    return port


def validate_timeout(timeout: float, name: str = "timeout") -> float:
    """Validate timeout value."""
    if timeout <= 0:
        raise ValueError(f"Invalid {name}: {timeout}. Must be positive.")
    return timeout


def validate_path(path: Path, name: str = "path", must_exist: bool = False) -> Path:
    """Validate path."""
    if must_exist and not path.exists():
        raise ValueError(f"Invalid {name}: {path}. Path does not exist.")
    return path


# =============================================================================
# ENVIRONMENT EXPORT (for subprocess communication)
# =============================================================================


def export_config_to_env(config: Optional[TrinityConfig] = None) -> Dict[str, str]:
    """
    Export configuration to environment variables for subprocess inheritance.

    Args:
        config: Optional config, uses global if not provided

    Returns:
        Dictionary of environment variable key-value pairs
    """
    if config is None:
        config = get_config()

    env_vars = {
        "TRINITY_DIR": str(config.trinity_dir),
        "TRINITY_ENABLED": str(config.enabled).lower(),
        "TRINITY_DEBUG": str(config.debug_mode).lower(),

        "JARVIS_HOST": config.jarvis_endpoint.host,
        "JARVIS_PORT": str(config.jarvis_endpoint.port),
        "JARVIS_PRIME_HOST": config.jarvis_prime_endpoint.host,
        "JARVIS_PRIME_PORT": str(config.jarvis_prime_endpoint.port),
        "REACTOR_CORE_HOST": config.reactor_core_endpoint.host,
        "REACTOR_CORE_PORT": str(config.reactor_core_endpoint.port),

        "TRINITY_HEARTBEAT_INTERVAL": str(config.health.heartbeat_interval),
        "TRINITY_HEARTBEAT_TIMEOUT": str(config.health.heartbeat_timeout),
        "TRINITY_HEALTH_CHECK_INTERVAL": str(config.health.health_check_interval),

        "TRINITY_MAX_RETRIES": str(config.retry.max_retries),
        "TRINITY_RETRY_DELAY": str(config.retry.initial_delay),
        "TRINITY_RETRY_MAX_DELAY": str(config.retry.max_delay),
        "TRINITY_JITTER_FACTOR": str(config.retry.jitter_factor),

        "TRINITY_COMMAND_TIMEOUT": str(config.timeouts.command_execution),
        "TRINITY_FILE_OPERATION_TIMEOUT": str(config.timeouts.file_operation),
        "TRINITY_MODEL_LOAD_TIMEOUT": str(config.timeouts.model_load),
    }

    # Update os.environ for subprocess inheritance
    os.environ.update(env_vars)

    return env_vars


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

# Initialize configuration on module load for early validation
try:
    _startup_config = get_config()
    logger.debug(f"[TrinityConfig] Module loaded, trinity_dir={_startup_config.trinity_dir}")
except Exception as e:
    logger.error(f"[TrinityConfig] Failed to initialize: {e}")

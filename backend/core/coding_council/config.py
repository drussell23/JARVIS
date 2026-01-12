"""
v77.4: Unified Trinity Configuration System
============================================

Advanced, centralized configuration management for the Coding Council
and Trinity cross-repo integration. This module eliminates hardcoding
and provides:

- Dynamic configuration discovery
- Environment-aware defaults
- Runtime reconfiguration
- Configuration validation
- Cascading configuration (env → file → defaults)
- Type-safe access with dataclasses
- Hot-reload capability

Author: JARVIS v77.4
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import socket
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TypeVar, Union
from functools import lru_cache, cached_property
import weakref

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration Discovery
# =============================================================================

class ConfigDiscovery:
    """
    Intelligent configuration discovery system.

    Automatically detects:
    - Repository locations via git
    - Available ports
    - System capabilities
    - Running services
    """

    _instance: Optional["ConfigDiscovery"] = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._cache: Dict[str, Any] = {}
        self._cache_ttl: Dict[str, float] = {}
        self._watchers: List[Callable] = []

    @staticmethod
    def _find_repo_root(start_path: Path, marker: str = ".git") -> Optional[Path]:
        """Find repository root by walking up directory tree."""
        current = start_path.resolve()
        for _ in range(10):  # Max 10 levels up
            if (current / marker).exists():
                return current
            if current.parent == current:
                break
            current = current.parent
        return None

    @staticmethod
    def _find_sibling_repos(base_path: Path, patterns: Dict[str, List[str]]) -> Dict[str, Path]:
        """
        Find sibling repositories based on characteristic files.

        Args:
            base_path: Starting directory (usually current repo root)
            patterns: Dict mapping repo name to list of characteristic files

        Returns:
            Dict mapping repo name to discovered path
        """
        discovered = {}
        parent = base_path.parent

        if not parent.exists():
            return discovered

        # Check all sibling directories
        for sibling in parent.iterdir():
            if not sibling.is_dir() or sibling.name.startswith('.'):
                continue

            # Check each pattern set
            for repo_name, markers in patterns.items():
                if repo_name in discovered:
                    continue

                # Check if this directory matches the pattern
                matches = sum(1 for m in markers if (sibling / m).exists())
                if matches >= len(markers) * 0.5:  # At least 50% of markers present
                    discovered[repo_name] = sibling

        return discovered

    @staticmethod
    def is_port_available(port: int, host: str = "127.0.0.1") -> bool:
        """Check if a port is available for binding."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.bind((host, port))
            sock.close()
            return True
        except OSError:
            return False

    @staticmethod
    def find_available_port(start_port: int, max_attempts: int = 100) -> int:
        """Find an available port starting from start_port."""
        for offset in range(max_attempts):
            port = start_port + offset
            if ConfigDiscovery.is_port_available(port):
                return port
        return start_port  # Fallback, will fail later

    def discover_trinity_repos(self) -> Dict[str, Path]:
        """
        Dynamically discover Trinity repository locations.

        Uses multiple strategies:
        1. Environment variables (highest priority)
        2. Relative path detection from current repo
        3. Common development locations
        4. Git worktree discovery
        """
        # Get current file's location to find JARVIS repo
        current_file = Path(__file__).resolve()
        jarvis_root = self._find_repo_root(current_file)

        if not jarvis_root:
            jarvis_root = Path.cwd()

        # Define characteristic files for each repo
        repo_patterns = {
            "j_prime": ["jarvis_prime/__init__.py", "jarvis_prime/core"],
            "reactor_core": ["reactor_core/__init__.py", "reactor_core/orchestration"],
        }

        repos: Dict[str, Path] = {"jarvis": jarvis_root}

        # Check environment variables first
        env_mappings = {
            "jarvis": "JARVIS_REPO",
            "j_prime": "J_PRIME_REPO",
            "reactor_core": "REACTOR_CORE_REPO",
        }

        for repo_name, env_var in env_mappings.items():
            env_path = os.getenv(env_var)
            if env_path:
                path = Path(env_path)
                if path.exists():
                    repos[repo_name] = path.resolve()

        # Discover sibling repos if not found via env
        if len(repos) < 3:
            discovered = self._find_sibling_repos(jarvis_root, repo_patterns)
            for name, path in discovered.items():
                if name not in repos:
                    repos[name] = path

        # Try common naming patterns in parent directory
        parent = jarvis_root.parent
        name_patterns = {
            "j_prime": ["jarvis-prime", "j-prime", "jprime"],
            "reactor_core": ["reactor-core", "reactorcore", "reactor_core"],
        }

        for repo_name, names in name_patterns.items():
            if repo_name not in repos:
                for name in names:
                    candidate = parent / name
                    if candidate.exists() and (candidate / ".git").exists():
                        repos[repo_name] = candidate
                        break

        return repos

    def get_system_info(self) -> Dict[str, Any]:
        """Get system information for configuration decisions."""
        return {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "python_version": sys.version,
            "hostname": socket.gethostname(),
            "cpu_count": os.cpu_count() or 1,
            "home_dir": str(Path.home()),
            "cwd": str(Path.cwd()),
        }


# =============================================================================
# Configuration Data Classes
# =============================================================================

@dataclass
class TrinityRepoConfig:
    """Configuration for a single Trinity repository."""
    name: str
    path: Path
    enabled: bool = True
    priority: int = 1  # Lower = higher priority
    health_check_interval: float = 5.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "enabled": self.enabled,
            "priority": self.priority,
            "health_check_interval": self.health_check_interval,
        }


@dataclass
class PortConfig:
    """Port configuration with fallback support."""
    name: str
    preferred: int
    fallback_range: int = 10
    _resolved: Optional[int] = field(default=None, repr=False)

    @property
    def port(self) -> int:
        """Get resolved port (finds available if preferred is taken)."""
        if self._resolved is not None:
            return self._resolved

        if ConfigDiscovery.is_port_available(self.preferred):
            self._resolved = self.preferred
        else:
            self._resolved = ConfigDiscovery.find_available_port(
                self.preferred,
                self.fallback_range
            )
        return self._resolved

    def reset(self) -> None:
        """Reset resolved port for re-discovery."""
        self._resolved = None


@dataclass
class TimeoutConfig:
    """Timeout configuration with cascading defaults."""
    default: float = 30.0
    startup: float = 60.0
    shutdown: float = 30.0
    health_check: float = 5.0
    api_call: float = 120.0
    sync_operation: float = 10.0
    heartbeat: float = 3.0
    command_ack: float = 5.0

    def get(self, operation: str) -> float:
        """Get timeout for a specific operation."""
        return getattr(self, operation, self.default)


@dataclass
class RetryConfig:
    """Retry configuration with exponential backoff."""
    max_attempts: int = 3
    initial_delay: float = 0.5
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True

    def get_delay(self, attempt: int) -> float:
        """Calculate delay for a given attempt number."""
        import random
        delay = min(
            self.initial_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        if self.jitter:
            delay *= (0.5 + random.random())
        return delay


@dataclass
class APIKeyConfig:
    """API key configuration with validation and fallback."""
    name: str
    env_var: str
    required: bool = False
    pattern: Optional[str] = None  # Regex pattern for validation
    _value: Optional[str] = field(default=None, repr=False)

    @property
    def value(self) -> Optional[str]:
        """Get API key value from environment."""
        if self._value is not None:
            return self._value
        return os.getenv(self.env_var)

    @property
    def is_set(self) -> bool:
        """Check if API key is set."""
        return bool(self.value)

    @property
    def is_valid(self) -> bool:
        """Validate API key format."""
        val = self.value
        if not val:
            return not self.required

        if self.pattern:
            import re
            return bool(re.match(self.pattern, val))
        return True

    def get_masked(self) -> str:
        """Get masked version for logging."""
        val = self.value
        if not val:
            return "(not set)"
        if len(val) > 12:
            return f"{val[:8]}...{val[-4:]}"
        return "***"


@dataclass
class GracefulDegradationConfig:
    """Configuration for graceful degradation when components fail."""
    enabled: bool = True
    allow_no_api_key: bool = True  # Continue without API key (reduced functionality)
    allow_partial_trinity: bool = True  # Continue if some repos unavailable
    allow_stale_cache: bool = True  # Use stale cache if fresh data unavailable
    fallback_to_local: bool = True  # Fall back to local-only mode
    max_degradation_level: int = 3  # 0=full, 1=reduced, 2=minimal, 3=emergency


# =============================================================================
# Main Configuration Class
# =============================================================================

@dataclass
class UnifiedTrinityConfig:
    """
    Unified configuration for the entire Coding Council and Trinity system.

    This is the single source of truth for all configuration values.
    """

    # Core settings
    enabled: bool = True
    debug: bool = False
    log_level: str = "INFO"

    # Trinity repos (dynamically discovered)
    repos: Dict[str, TrinityRepoConfig] = field(default_factory=dict)

    # Ports
    jarvis_api_port: PortConfig = field(default_factory=lambda: PortConfig("jarvis_api", 8010))
    lsp_server_port: PortConfig = field(default_factory=lambda: PortConfig("lsp_server", 9257))
    websocket_port: PortConfig = field(default_factory=lambda: PortConfig("websocket", 9258))

    # Directories
    data_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis")
    trinity_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "trinity")
    cache_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "cache")
    logs_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "logs")

    # Intervals (in seconds)
    sync_interval: float = 5.0
    heartbeat_interval: float = 10.0
    health_check_interval: float = 15.0
    stale_threshold: float = 60.0  # Consider component stale after this

    # Timeouts
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)

    # Retry settings
    retry: RetryConfig = field(default_factory=RetryConfig)

    # API Keys
    anthropic_key: APIKeyConfig = field(default_factory=lambda: APIKeyConfig(
        name="Anthropic",
        env_var="ANTHROPIC_API_KEY",
        required=False,  # Not required - graceful degradation
        pattern=r"^sk-ant-|^sk-"
    ))

    # Graceful degradation
    degradation: GracefulDegradationConfig = field(default_factory=GracefulDegradationConfig)

    # Feature flags
    ide_bridge_enabled: bool = True
    trinity_sync_enabled: bool = True
    voice_commands_enabled: bool = True
    auto_recovery_enabled: bool = True

    # Performance tuning
    max_concurrent_operations: int = 10
    max_queue_size: int = 10000
    cache_size: int = 5000

    # Computed properties
    _discovery: ConfigDiscovery = field(default_factory=ConfigDiscovery, repr=False)
    _initialized: bool = field(default=False, repr=False)
    _callbacks: List[Callable] = field(default_factory=list, repr=False)

    def __post_init__(self):
        """Initialize after dataclass creation."""
        if not self._initialized:
            self._initialize()

    def _initialize(self) -> None:
        """Run initialization logic."""
        # Discover repos if not set
        if not self.repos:
            discovered = self._discovery.discover_trinity_repos()
            for name, path in discovered.items():
                self.repos[name] = TrinityRepoConfig(name=name, path=path)

        # Create directories
        for dir_path in [self.data_dir, self.trinity_dir, self.cache_dir, self.logs_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)

        # Load from environment
        self._load_from_env()

        self._initialized = True

    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        env_mappings = {
            "CODING_COUNCIL_ENABLED": ("enabled", bool),
            "CODING_COUNCIL_DEBUG": ("debug", bool),
            "CODING_COUNCIL_LOG_LEVEL": ("log_level", str),
            "IDE_BRIDGE_ENABLED": ("ide_bridge_enabled", bool),
            "TRINITY_SYNC_ENABLED": ("trinity_sync_enabled", bool),
            "TRINITY_SYNC_INTERVAL": ("sync_interval", float),
            "TRINITY_HEARTBEAT_INTERVAL": ("heartbeat_interval", float),
            "TRINITY_STALE_THRESHOLD": ("stale_threshold", float),
        }

        for env_var, (attr, type_) in env_mappings.items():
            val = os.getenv(env_var)
            if val is not None:
                try:
                    if type_ == bool:
                        setattr(self, attr, val.lower() in ("true", "1", "yes"))
                    else:
                        setattr(self, attr, type_(val))
                except (ValueError, TypeError):
                    pass

    def get_repo(self, name: str) -> Optional[TrinityRepoConfig]:
        """Get configuration for a specific repo."""
        return self.repos.get(name)

    def get_healthy_repos(self) -> List[TrinityRepoConfig]:
        """Get list of repos that exist and are enabled."""
        return [
            repo for repo in self.repos.values()
            if repo.enabled and repo.path.exists()
        ]

    @property
    def degradation_level(self) -> int:
        """
        Calculate current degradation level based on system state.

        0 = Full functionality (all components healthy)
        1 = Reduced (some non-critical components missing)
        2 = Minimal (core functionality only)
        3 = Emergency (basic operation only)
        """
        level = 0

        # Check API key
        if not self.anthropic_key.is_set:
            level = max(level, 1)

        # Check repos
        healthy_repos = len(self.get_healthy_repos())
        total_repos = len(self.repos)
        if healthy_repos < total_repos:
            level = max(level, 1)
        if healthy_repos < total_repos // 2:
            level = max(level, 2)
        if healthy_repos == 0:
            level = max(level, 3)

        return min(level, self.degradation.max_degradation_level)

    @property
    def can_use_ai(self) -> bool:
        """Check if AI functionality is available."""
        return self.anthropic_key.is_set and self.anthropic_key.is_valid

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization/logging."""
        return {
            "enabled": self.enabled,
            "debug": self.debug,
            "repos": {name: repo.to_dict() for name, repo in self.repos.items()},
            "ports": {
                "jarvis_api": self.jarvis_api_port.port,
                "lsp_server": self.lsp_server_port.port,
                "websocket": self.websocket_port.port,
            },
            "intervals": {
                "sync": self.sync_interval,
                "heartbeat": self.heartbeat_interval,
                "health_check": self.health_check_interval,
                "stale_threshold": self.stale_threshold,
            },
            "api_keys": {
                "anthropic": self.anthropic_key.get_masked(),
            },
            "degradation_level": self.degradation_level,
            "can_use_ai": self.can_use_ai,
            "feature_flags": {
                "ide_bridge": self.ide_bridge_enabled,
                "trinity_sync": self.trinity_sync_enabled,
                "voice_commands": self.voice_commands_enabled,
                "auto_recovery": self.auto_recovery_enabled,
            },
        }

    def register_callback(self, callback: Callable[["UnifiedTrinityConfig"], None]) -> None:
        """Register callback for configuration changes."""
        self._callbacks.append(callback)

    def _notify_callbacks(self) -> None:
        """Notify all registered callbacks of configuration change."""
        for callback in self._callbacks:
            try:
                callback(self)
            except Exception as e:
                logger.warning(f"Config callback error: {e}")


# =============================================================================
# Singleton Access
# =============================================================================

_config: Optional[UnifiedTrinityConfig] = None
_config_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_config() -> UnifiedTrinityConfig:
    """Get the global configuration instance (sync version)."""
    global _config
    if _config is None:
        _config = UnifiedTrinityConfig()
    return _config


async def get_config_async() -> UnifiedTrinityConfig:
    """Get the global configuration instance (async version)."""
    global _config
    async with _config_lock:
        if _config is None:
            _config = UnifiedTrinityConfig()
        return _config


def reset_config() -> None:
    """Reset configuration (mainly for testing)."""
    global _config
    _config = None


# =============================================================================
# Configuration Validation
# =============================================================================

class ConfigValidator:
    """Validates configuration for common issues."""

    @staticmethod
    def validate(config: UnifiedTrinityConfig) -> List[str]:
        """
        Validate configuration and return list of issues.

        Returns empty list if configuration is valid.
        """
        issues = []

        # Check required directories
        for dir_name in ["data_dir", "trinity_dir"]:
            dir_path = getattr(config, dir_name)
            if not dir_path.exists():
                try:
                    dir_path.mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    issues.append(f"Cannot create {dir_name}: {e}")

        # Check repos
        if not config.repos:
            issues.append("No Trinity repos discovered")
        else:
            for name, repo in config.repos.items():
                if not repo.path.exists():
                    issues.append(f"Repo {name} path does not exist: {repo.path}")

        # Check ports
        for port_name in ["jarvis_api_port", "lsp_server_port", "websocket_port"]:
            port_config = getattr(config, port_name)
            if port_config.port != port_config.preferred:
                issues.append(
                    f"Port {port_name} using fallback {port_config.port} "
                    f"(preferred {port_config.preferred} in use)"
                )

        # Check API key
        if not config.anthropic_key.is_set:
            if config.anthropic_key.required:
                issues.append("ANTHROPIC_API_KEY is required but not set")
            else:
                # This is a warning, not an error
                pass
        elif not config.anthropic_key.is_valid:
            issues.append("ANTHROPIC_API_KEY format appears invalid")

        return issues

    @staticmethod
    def validate_and_log(config: UnifiedTrinityConfig) -> bool:
        """Validate configuration and log any issues."""
        issues = ConfigValidator.validate(config)

        for issue in issues:
            logger.warning(f"[Config] {issue}")

        return len(issues) == 0


# =============================================================================
# Convenience Exports
# =============================================================================

# Quick access to common config values
def get_trinity_repos() -> Dict[str, Path]:
    """Get discovered Trinity repo paths."""
    config = get_config()
    return {name: repo.path for name, repo in config.repos.items()}


def get_port(name: str) -> int:
    """Get resolved port by name."""
    config = get_config()
    port_attr = f"{name}_port"
    port_config = getattr(config, port_attr, None)
    if port_config:
        return port_config.port
    raise ValueError(f"Unknown port: {name}")


def get_timeout(operation: str) -> float:
    """Get timeout for an operation."""
    return get_config().timeouts.get(operation)


def can_use_ai() -> bool:
    """Check if AI functionality is available."""
    return get_config().can_use_ai


def get_degradation_level() -> int:
    """Get current degradation level."""
    return get_config().degradation_level

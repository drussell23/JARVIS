"""
JARVIS Kernel Configuration v1.0.0
===================================

Enterprise-grade configuration management for the JARVIS kernel.
Zero hardcoding - all values from environment variables with intelligent defaults.

This module provides:
1. SystemKernelConfig - Main kernel configuration
2. Environment variable loading with type safety
3. Dynamic reconfiguration support
4. Configuration validation

Environment Variables:
    JARVIS_MODE                 - Startup mode (development/production)
    JARVIS_DEBUG                - Enable debug mode
    JARVIS_SKIP_DOCKER          - Skip Docker initialization
    JARVIS_SKIP_GCP             - Skip GCP initialization
    JARVIS_SKIP_TRINITY         - Skip Trinity cross-repo startup
    JARVIS_BACKEND_PORT         - Backend API port
    JARVIS_FRONTEND_PORT        - Frontend port
    JARVIS_HOT_RELOAD           - Enable hot reload
    JARVIS_VOICE_ENABLED        - Enable voice features
    JARVIS_LOADING_SERVER_PORT  - Loading server port

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import logging
import os
import platform
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================

class StartupMode(Enum):
    """Kernel startup modes."""
    DEVELOPMENT = "development"
    PRODUCTION = "production"
    MINIMAL = "minimal"  # Minimal startup for testing


class HardwareProfile(Enum):
    """Hardware profile for adaptive startup."""
    CLOUD_ONLY = auto()   # < 16GB RAM - use GCP exclusively
    SLIM = auto()         # 16-30GB RAM - slim mode / deferred heavy
    FULL = auto()         # 30-64GB RAM - full with staged loading
    UNLIMITED = auto()    # 64GB+ RAM - can run everything


# =============================================================================
# ENVIRONMENT HELPERS
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
        logger.warning(f"[KernelConfig] Invalid int for {key}: {value}, using default: {default}")
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment with validation."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning(f"[KernelConfig] Invalid float for {key}: {value}, using default: {default}")
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
# SYSTEM KERNEL CONFIGURATION
# =============================================================================

@dataclass
class SystemKernelConfig:
    """
    Configuration for the JARVIS System Kernel.
    
    All values are loaded from environment variables with intelligent defaults.
    No hardcoding - the configuration is fully dynamic.
    
    Usage:
        config = SystemKernelConfig()
        print(config.backend_port)  # 8010 (or JARVIS_BACKEND_PORT env var)
        print(config.mode)  # StartupMode.DEVELOPMENT (or JARVIS_MODE env var)
    """
    
    # =========================================================================
    # Core Settings
    # =========================================================================
    
    mode: StartupMode = field(default_factory=lambda: StartupMode(
        _env_str("JARVIS_MODE", "development")
    ) if _env_str("JARVIS_MODE", "development") in [m.value for m in StartupMode] else StartupMode.DEVELOPMENT)
    
    debug: bool = field(default_factory=lambda: _env_bool("JARVIS_DEBUG", False))
    verbose: bool = field(default_factory=lambda: _env_bool("JARVIS_VERBOSE", False))
    
    # =========================================================================
    # Paths
    # =========================================================================
    
    project_root: Path = field(default_factory=lambda: _env_path(
        "JARVIS_PROJECT_ROOT", 
        Path(__file__).parent.parent.parent  # backend/kernel/config.py -> project root
    ))
    
    jarvis_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_DIR",
        Path.home() / ".jarvis"
    ))
    
    locks_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_LOCKS_DIR",
        Path.home() / ".jarvis" / "locks"
    ))
    
    cache_dir: Path = field(default_factory=lambda: _env_path(
        "JARVIS_CACHE_DIR",
        Path.home() / ".jarvis" / "cache"
    ))
    
    # =========================================================================
    # Ports
    # =========================================================================
    
    backend_port: int = field(default_factory=lambda: _env_int("JARVIS_BACKEND_PORT", 8010))
    frontend_port: int = field(default_factory=lambda: _env_int("JARVIS_FRONTEND_PORT", 3000))
    loading_server_port: int = field(default_factory=lambda: _env_int("JARVIS_LOADING_SERVER_PORT", 3001))
    websocket_port: int = field(default_factory=lambda: _env_int("JARVIS_WEBSOCKET_PORT", 8765))
    
    # =========================================================================
    # Feature Flags
    # =========================================================================
    
    skip_docker: bool = field(default_factory=lambda: _env_bool("JARVIS_SKIP_DOCKER", False))
    skip_gcp: bool = field(default_factory=lambda: _env_bool("JARVIS_SKIP_GCP", False))
    skip_trinity: bool = field(default_factory=lambda: _env_bool("JARVIS_SKIP_TRINITY", False))
    skip_frontend: bool = field(default_factory=lambda: _env_bool("JARVIS_SKIP_FRONTEND", False))
    skip_intelligence: bool = field(default_factory=lambda: _env_bool("JARVIS_SKIP_INTELLIGENCE", False))
    
    trinity_enabled: bool = field(default_factory=lambda: _env_bool("JARVIS_TRINITY_ENABLED", True))
    hot_reload: bool = field(default_factory=lambda: _env_bool("JARVIS_HOT_RELOAD", True))
    voice_enabled: bool = field(default_factory=lambda: _env_bool("JARVIS_VOICE_ENABLED", True))
    
    # =========================================================================
    # Trinity Cross-Repo Settings
    # =========================================================================
    
    jarvis_prime_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_PRIME_PATH",
        Path.home() / "Documents" / "repos" / "jarvis-prime"
    ))
    
    reactor_core_path: Path = field(default_factory=lambda: _env_path(
        "REACTOR_CORE_PATH",
        Path.home() / "Documents" / "repos" / "reactor-core"
    ))
    
    jarvis_prime_port: int = field(default_factory=lambda: _env_int("JARVIS_PRIME_PORT", 8001))
    reactor_core_port: int = field(default_factory=lambda: _env_int("REACTOR_CORE_PORT", 8090))
    
    # =========================================================================
    # Timeouts (in seconds)
    # =========================================================================
    
    startup_timeout: float = field(default_factory=lambda: _env_float("JARVIS_STARTUP_TIMEOUT", 300.0))
    health_check_timeout: float = field(default_factory=lambda: _env_float("JARVIS_HEALTH_CHECK_TIMEOUT", 10.0))
    graceful_shutdown_timeout: float = field(default_factory=lambda: _env_float("JARVIS_SHUTDOWN_TIMEOUT", 30.0))
    process_spawn_timeout: float = field(default_factory=lambda: _env_float("JARVIS_SPAWN_TIMEOUT", 60.0))
    
    # =========================================================================
    # Resource Limits
    # =========================================================================
    
    max_concurrent_processes: int = field(default_factory=lambda: _env_int("JARVIS_MAX_PROCESSES", 10))
    max_memory_percent: float = field(default_factory=lambda: _env_float("JARVIS_MAX_MEMORY_PCT", 80.0))
    
    # =========================================================================
    # Retry Settings
    # =========================================================================
    
    max_retries: int = field(default_factory=lambda: _env_int("JARVIS_MAX_RETRIES", 3))
    retry_delay: float = field(default_factory=lambda: _env_float("JARVIS_RETRY_DELAY", 1.0))
    retry_max_delay: float = field(default_factory=lambda: _env_float("JARVIS_RETRY_MAX_DELAY", 30.0))
    
    # =========================================================================
    # Hardware Detection (computed at runtime)
    # =========================================================================
    
    hardware_profile: Optional[HardwareProfile] = None
    
    def __post_init__(self):
        """Post-initialization: detect hardware and create directories."""
        # Ensure directories exist
        self.jarvis_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Detect hardware profile
        self._detect_hardware_profile()
        
        logger.debug(
            f"[KernelConfig] Initialized: mode={self.mode.value}, "
            f"debug={self.debug}, profile={self.hardware_profile}"
        )
    
    def _detect_hardware_profile(self) -> None:
        """Detect hardware profile based on system resources."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            total_gb = mem.total / (1024 ** 3)
            
            if total_gb < 16:
                self.hardware_profile = HardwareProfile.CLOUD_ONLY
            elif total_gb < 30:
                self.hardware_profile = HardwareProfile.SLIM
            elif total_gb < 64:
                self.hardware_profile = HardwareProfile.FULL
            else:
                self.hardware_profile = HardwareProfile.UNLIMITED
                
        except ImportError:
            # Default to SLIM if psutil not available
            self.hardware_profile = HardwareProfile.SLIM
        except Exception as e:
            logger.warning(f"[KernelConfig] Hardware detection failed: {e}")
            self.hardware_profile = HardwareProfile.SLIM
    
    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.mode == StartupMode.PRODUCTION
    
    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.mode == StartupMode.DEVELOPMENT
    
    @property
    def is_apple_silicon(self) -> bool:
        """Check if running on Apple Silicon."""
        return (
            platform.system() == "Darwin" and
            platform.machine() in ("arm64", "aarch64")
        )
    
    @property
    def can_run_local_ml(self) -> bool:
        """Check if local ML is feasible based on hardware profile."""
        return self.hardware_profile not in (HardwareProfile.CLOUD_ONLY, None)
    
    @property
    def should_use_gcp(self) -> bool:
        """Check if GCP should be used for ML inference."""
        return (
            not self.skip_gcp and
            self.hardware_profile in (HardwareProfile.CLOUD_ONLY, HardwareProfile.SLIM)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary for serialization."""
        return {
            "mode": self.mode.value,
            "debug": self.debug,
            "verbose": self.verbose,
            "project_root": str(self.project_root),
            "jarvis_dir": str(self.jarvis_dir),
            "backend_port": self.backend_port,
            "frontend_port": self.frontend_port,
            "trinity_enabled": self.trinity_enabled,
            "hardware_profile": self.hardware_profile.name if self.hardware_profile else None,
            "is_apple_silicon": self.is_apple_silicon,
            "can_run_local_ml": self.can_run_local_ml,
        }
    
    def __repr__(self) -> str:
        return f"SystemKernelConfig(mode={self.mode.value}, debug={self.debug})"


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_config_instance: Optional[SystemKernelConfig] = None


def get_config() -> SystemKernelConfig:
    """
    Get the singleton configuration instance.
    
    Returns:
        SystemKernelConfig instance with current configuration.
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = SystemKernelConfig()
    return _config_instance


def reset_config() -> None:
    """Reset the configuration singleton (for testing)."""
    global _config_instance
    _config_instance = None


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.debug("[KernelConfig] Module loaded")

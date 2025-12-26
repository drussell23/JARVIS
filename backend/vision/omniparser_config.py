"""
OmniParser Unified Configuration System
========================================

Centralized configuration for OmniParser across JARVIS, JARVIS Prime, and Reactor Core.

Features:
- Single source of truth for OmniParser settings
- Cross-repo configuration sharing via ~/.jarvis/omniparser_config.json
- Runtime configuration updates
- Environment variable overrides
- Intelligent defaults

Author: JARVIS AI System
Version: 6.2.0
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ============================================================================
# Constants
# ============================================================================

CONFIG_FILE = Path.home() / ".jarvis" / "omniparser_config.json"
DEFAULT_CONFIG_FILE = Path(__file__).parent.parent / "config" / "omniparser_defaults.json"


# ============================================================================
# Configuration Dataclass
# ============================================================================

@dataclass
class OmniParserConfig:
    """OmniParser configuration settings."""

    # Core settings
    enabled: bool = True  # Enable OmniParser (with fallback modes)
    auto_mode_selection: bool = True  # Auto-select best available parser
    preferred_mode: str = "auto"  # auto, omniparser, claude_vision, ocr, disabled

    # Performance settings
    cache_enabled: bool = True
    cache_size: int = 1000
    cache_ttl_seconds: int = 3600
    max_workers: int = 4
    parse_timeout: float = 10.0

    # Parser-specific settings
    omniparser_device: str = "cpu"  # cpu, cuda, mps
    omniparser_model_version: str = "v2"
    claude_vision_model: str = "claude-3-5-sonnet-20241022"
    ocr_confidence_threshold: float = 50.0

    # Element detection settings
    min_element_confidence: float = 0.5
    detect_buttons: bool = True
    detect_icons: bool = True
    detect_text: bool = True
    detect_inputs: bool = True
    detect_menus: bool = True

    # Cross-repo settings
    share_cache_across_repos: bool = True
    emit_events_to_bridge: bool = True

    # Logging
    log_level: str = "INFO"
    log_parse_times: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OmniParserConfig":
        """Create from dictionary."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self, filepath: Optional[Path] = None) -> None:
        """Save configuration to file."""
        filepath = filepath or CONFIG_FILE
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)

        logger.info(f"[OMNIPARSER CONFIG] Saved to {filepath}")

    @classmethod
    def load(cls, filepath: Optional[Path] = None) -> "OmniParserConfig":
        """Load configuration from file."""
        filepath = filepath or CONFIG_FILE

        if not filepath.exists():
            logger.info("[OMNIPARSER CONFIG] No config file found, using defaults")
            return cls()

        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            logger.info(f"[OMNIPARSER CONFIG] Loaded from {filepath}")
            return cls.from_dict(data)

        except Exception as e:
            logger.warning(f"[OMNIPARSER CONFIG] Failed to load: {e}, using defaults")
            return cls()


# ============================================================================
# Configuration Manager
# ============================================================================

class OmniParserConfigManager:
    """
    Manages OmniParser configuration across all repos.

    Features:
    - Loads from config file
    - Environment variable overrides
    - Runtime updates
    - Cross-repo sharing
    """

    def __init__(self):
        """Initialize configuration manager."""
        self._config: Optional[OmniParserConfig] = None
        self._initialized = False

    def get_config(self, reload: bool = False) -> OmniParserConfig:
        """
        Get current configuration.

        Args:
            reload: Reload from disk

        Returns:
            OmniParserConfig instance
        """
        if self._config is None or reload:
            self._load_config()

        return self._config

    def _load_config(self) -> None:
        """Load configuration from file and apply environment overrides."""
        # Load from file
        self._config = OmniParserConfig.load()

        # Apply environment variable overrides
        self._apply_env_overrides()

        logger.info("[OMNIPARSER CONFIG] Configuration loaded successfully")
        logger.info(f"[OMNIPARSER CONFIG] Enabled: {self._config.enabled}")
        logger.info(f"[OMNIPARSER CONFIG] Auto-mode: {self._config.auto_mode_selection}")
        logger.info(f"[OMNIPARSER CONFIG] Preferred: {self._config.preferred_mode}")
        logger.info(f"[OMNIPARSER CONFIG] Cache: {self._config.cache_enabled} ({self._config.cache_size} entries)")

        self._initialized = True

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides to configuration."""
        if not self._config:
            return

        # OMNIPARSER_ENABLED
        env_enabled = os.getenv("OMNIPARSER_ENABLED")
        if env_enabled is not None:
            self._config.enabled = env_enabled.lower() == "true"
            logger.info(f"[OMNIPARSER CONFIG] Override: enabled={self._config.enabled} (from env)")

        # OMNIPARSER_MODE
        env_mode = os.getenv("OMNIPARSER_MODE")
        if env_mode is not None:
            self._config.preferred_mode = env_mode
            logger.info(f"[OMNIPARSER CONFIG] Override: preferred_mode={env_mode} (from env)")

        # OMNIPARSER_CACHE_ENABLED
        env_cache = os.getenv("OMNIPARSER_CACHE_ENABLED")
        if env_cache is not None:
            self._config.cache_enabled = env_cache.lower() == "true"
            logger.info(f"[OMNIPARSER CONFIG] Override: cache_enabled={self._config.cache_enabled} (from env)")

        # OMNIPARSER_DEVICE
        env_device = os.getenv("OMNIPARSER_DEVICE")
        if env_device is not None:
            self._config.omniparser_device = env_device
            logger.info(f"[OMNIPARSER CONFIG] Override: device={env_device} (from env)")

        # OMNIPARSER_LOG_LEVEL
        env_log_level = os.getenv("OMNIPARSER_LOG_LEVEL")
        if env_log_level is not None:
            self._config.log_level = env_log_level
            logger.info(f"[OMNIPARSER CONFIG] Override: log_level={env_log_level} (from env)")

    def update_config(self, **kwargs) -> None:
        """
        Update configuration at runtime.

        Args:
            **kwargs: Configuration fields to update
        """
        if not self._config:
            self._load_config()

        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
                logger.info(f"[OMNIPARSER CONFIG] Updated: {key}={value}")
            else:
                logger.warning(f"[OMNIPARSER CONFIG] Unknown config key: {key}")

        # Save updated config
        self._config.save()

    def reset_to_defaults(self) -> None:
        """Reset configuration to defaults."""
        self._config = OmniParserConfig()
        self._config.save()
        logger.info("[OMNIPARSER CONFIG] Reset to defaults")

    def export_config(self, filepath: Path) -> None:
        """Export current configuration to file."""
        if not self._config:
            self._load_config()

        self._config.save(filepath)
        logger.info(f"[OMNIPARSER CONFIG] Exported to {filepath}")

    def import_config(self, filepath: Path) -> None:
        """Import configuration from file."""
        self._config = OmniParserConfig.load(filepath)
        self._config.save()  # Save to main config location
        logger.info(f"[OMNIPARSER CONFIG] Imported from {filepath}")

    def get_stats(self) -> Dict[str, Any]:
        """Get configuration statistics."""
        if not self._config:
            self._load_config()

        return {
            "initialized": self._initialized,
            "config_file": str(CONFIG_FILE),
            "config_exists": CONFIG_FILE.exists(),
            "enabled": self._config.enabled,
            "preferred_mode": self._config.preferred_mode,
            "cache_enabled": self._config.cache_enabled,
        }


# ============================================================================
# Global Instance
# ============================================================================

_config_manager: Optional[OmniParserConfigManager] = None


def get_config_manager() -> OmniParserConfigManager:
    """Get or create global configuration manager."""
    global _config_manager

    if _config_manager is None:
        _config_manager = OmniParserConfigManager()

    return _config_manager


def get_config(reload: bool = False) -> OmniParserConfig:
    """Get current OmniParser configuration."""
    manager = get_config_manager()
    return manager.get_config(reload=reload)


def update_config(**kwargs) -> None:
    """Update OmniParser configuration."""
    manager = get_config_manager()
    manager.update_config(**kwargs)


# ============================================================================
# Convenience Functions
# ============================================================================

def is_enabled() -> bool:
    """Check if OmniParser is enabled."""
    config = get_config()
    return config.enabled


def get_preferred_mode() -> str:
    """Get preferred parser mode."""
    config = get_config()
    return config.preferred_mode


def is_cache_enabled() -> bool:
    """Check if caching is enabled."""
    config = get_config()
    return config.cache_enabled


def get_device() -> str:
    """Get compute device for OmniParser."""
    config = get_config()
    return config.omniparser_device


# ============================================================================
# CLI for Configuration Management
# ============================================================================

def cli_show_config():
    """Show current configuration."""
    config = get_config()
    print("\n" + "="*70)
    print("OMNIPARSER CONFIGURATION")
    print("="*70)

    for key, value in config.to_dict().items():
        print(f"{key:30s}: {value}")

    print("="*70)
    print(f"Config file: {CONFIG_FILE}")
    print(f"Exists: {CONFIG_FILE.exists()}")
    print("="*70 + "\n")


def cli_reset_config():
    """Reset configuration to defaults."""
    manager = get_config_manager()
    manager.reset_to_defaults()
    print("✅ Configuration reset to defaults")


def cli_update_config(key: str, value: str):
    """Update a configuration value."""
    # Try to convert value to appropriate type
    if value.lower() in ("true", "false"):
        value = value.lower() == "true"
    elif value.isdigit():
        value = int(value)
    elif "." in value:
        try:
            value = float(value)
        except ValueError:
            pass

    update_config(**{key: value})
    print(f"✅ Updated {key} = {value}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        cli_show_config()
    elif sys.argv[1] == "show":
        cli_show_config()
    elif sys.argv[1] == "reset":
        cli_reset_config()
    elif sys.argv[1] == "set" and len(sys.argv) == 4:
        cli_update_config(sys.argv[2], sys.argv[3])
    else:
        print("Usage:")
        print("  python omniparser_config.py                  # Show config")
        print("  python omniparser_config.py show             # Show config")
        print("  python omniparser_config.py reset            # Reset to defaults")
        print("  python omniparser_config.py set KEY VALUE    # Set config value")

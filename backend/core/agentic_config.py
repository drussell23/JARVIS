"""
JARVIS Agentic Configuration System

Provides dynamic, environment-aware configuration for the entire agentic system.
Eliminates hardcoding by using environment variables, config files, and runtime detection.

Features:
- Zero hardcoding - all values configurable
- Environment variable overrides
- Config file support (YAML/JSON)
- Runtime auto-detection of capabilities
- Validation and defaults
- Singleton pattern for global access

Usage:
    from backend.core.agentic_config import get_agentic_config, AgenticConfig

    config = get_agentic_config()
    model = config.computer_use.model_name
    timeout = config.computer_use.api_timeout
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from enum import Enum

logger = logging.getLogger(__name__)


class ConfigSource(Enum):
    """Source of configuration value."""
    DEFAULT = "default"
    ENV_VAR = "env_var"
    CONFIG_FILE = "config_file"
    RUNTIME = "runtime"


@dataclass
class ComputerUseConfig:
    """Configuration for Computer Use capabilities."""

    # Model settings
    model_name: str = field(default_factory=lambda: os.getenv(
        "JARVIS_COMPUTER_USE_MODEL", "claude-sonnet-4-20250514"
    ))

    # API settings
    api_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_API_TIMEOUT", "60.0"
    )))
    api_key: Optional[str] = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))

    # Execution settings
    max_actions_per_task: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_ACTIONS_PER_TASK", "20"
    )))
    action_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_ACTION_TIMEOUT", "30.0"
    )))

    # Screenshot settings
    screenshot_max_dimension: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_SCREENSHOT_MAX_DIM", "1568"
    )))
    capture_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CAPTURE_TIMEOUT", "10.0"
    )))

    # Thread pool settings
    thread_pool_workers: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_THREAD_POOL_WORKERS", "2"
    )))

    # Circuit breaker settings
    circuit_breaker_threshold: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_CIRCUIT_BREAKER_THRESHOLD", "3"
    )))
    circuit_breaker_recovery: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CIRCUIT_BREAKER_RECOVERY", "60.0"
    )))

    # Voice narration
    enable_narration: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_NARRATION", "true"
    ).lower() == "true")

    # Learning
    enable_learning: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_LEARNING", "true"
    ).lower() == "true")
    learned_positions_path: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "learned_ui_positions.json")


@dataclass
class UAEConfig:
    """Configuration for Unified Awareness Engine."""

    # Monitoring settings
    monitoring_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_UAE_MONITORING_INTERVAL", "5.0"
    )))

    # Context settings
    context_cache_ttl: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CONTEXT_CACHE_TTL", "30.0"
    )))

    # Integration weights
    context_base_weight: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CONTEXT_WEIGHT", "0.4"
    )))
    situation_base_weight: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_SITUATION_WEIGHT", "0.6"
    )))

    # Thresholds
    recency_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_RECENCY_THRESHOLD", "60.0"
    )))
    consistency_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CONSISTENCY_THRESHOLD", "0.8"
    )))
    min_confidence: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_MIN_CONFIDENCE", "0.5"
    )))

    # Knowledge base
    knowledge_base_path: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "uae_context.json")


@dataclass
class MultiSpaceVisionConfig:
    """Configuration for Multi-Space Vision Intelligence."""

    # Capture settings
    capture_all_spaces: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_CAPTURE_ALL_SPACES", "true"
    ).lower() == "true")

    # Yabai integration
    use_yabai: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_USE_YABAI", "true"
    ).lower() == "true")
    yabai_socket_path: Optional[str] = field(default_factory=lambda: os.getenv(
        "YABAI_SOCKET_PATH"
    ))

    # Space monitoring
    space_switch_delay: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_SPACE_SWITCH_DELAY", "0.3"
    )))
    max_spaces_to_capture: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_SPACES_CAPTURE", "16"
    )))

    # Window detection
    enable_window_detection: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_WINDOW_DETECTION", "true"
    ).lower() == "true")


@dataclass
class NeuralMeshConfig:
    """Configuration for Neural Mesh integration."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_NEURAL_MESH_ENABLED", "true"
    ).lower() == "true")

    # Communication bus
    message_queue_size: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MESSAGE_QUEUE_SIZE", "1000"
    )))

    # Agent settings
    max_concurrent_agents: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_CONCURRENT_AGENTS", "10"
    )))

    # Health monitoring
    health_check_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_HEALTH_CHECK_INTERVAL", "30.0"
    )))


@dataclass
class AutonomyConfig:
    """Configuration for Autonomous Agent."""

    # Mode settings
    default_mode: str = field(default_factory=lambda: os.getenv(
        "JARVIS_AUTONOMY_MODE", "supervised"
    ))

    # LLM settings
    reasoning_model: str = field(default_factory=lambda: os.getenv(
        "JARVIS_REASONING_MODEL", "claude-3-5-sonnet-20241022"
    ))
    temperature: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_LLM_TEMPERATURE", "0.7"
    )))
    max_tokens: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_LLM_MAX_TOKENS", "4096"
    )))

    # Reasoning settings
    max_reasoning_iterations: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_REASONING_ITERATIONS", "10"
    )))
    min_confidence_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_MIN_REASONING_CONFIDENCE", "0.4"
    )))

    # Tool settings
    max_concurrent_tools: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_CONCURRENT_TOOLS", "5"
    )))
    tool_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_TOOL_TIMEOUT", "30.0"
    )))

    # Memory settings
    enable_memory: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_MEMORY", "true"
    ).lower() == "true")
    working_memory_size: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_WORKING_MEMORY_SIZE", "100"
    )))

    # Safety settings
    max_actions_per_session: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_MAX_ACTIONS_SESSION", "100"
    )))
    require_permission_high_risk: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_REQUIRE_PERMISSION_HIGH_RISK", "true"
    ).lower() == "true")


@dataclass
class AgenticConfig:
    """
    Master configuration for the entire agentic system.

    Combines all subsystem configurations into a single coherent structure.
    """

    # Subsystem configurations
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)
    uae: UAEConfig = field(default_factory=UAEConfig)
    multi_space_vision: MultiSpaceVisionConfig = field(default_factory=MultiSpaceVisionConfig)
    neural_mesh: NeuralMeshConfig = field(default_factory=NeuralMeshConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)

    # Global settings
    debug_mode: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_DEBUG", "false"
    ).lower() == "true")
    log_level: str = field(default_factory=lambda: os.getenv(
        "JARVIS_LOG_LEVEL", "INFO"
    ))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "JARVIS_DATA_DIR", str(Path.home() / ".jarvis")
    )))

    # Feature flags
    enable_computer_use: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_COMPUTER_USE", "true"
    ).lower() == "true")
    enable_multi_space: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_MULTI_SPACE", "true"
    ).lower() == "true")
    enable_voice: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_VOICE", "true"
    ).lower() == "true")

    def __post_init__(self):
        """Ensure directories exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.computer_use.learned_positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.uae.knowledge_base_path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_file(cls, path: Path) -> "AgenticConfig":
        """Load configuration from a JSON or YAML file."""
        if not path.exists():
            logger.warning(f"Config file not found: {path}, using defaults")
            return cls()

        try:
            with open(path) as f:
                if path.suffix in ('.yml', '.yaml'):
                    import yaml
                    data = yaml.safe_load(f)
                else:
                    data = json.load(f)

            return cls._from_dict(data)
        except Exception as e:
            logger.error(f"Error loading config from {path}: {e}")
            return cls()

    @classmethod
    def _from_dict(cls, data: Dict[str, Any]) -> "AgenticConfig":
        """Create config from dictionary."""
        config = cls()

        # Map nested configs
        if 'computer_use' in data:
            for key, value in data['computer_use'].items():
                if hasattr(config.computer_use, key):
                    setattr(config.computer_use, key, value)

        if 'uae' in data:
            for key, value in data['uae'].items():
                if hasattr(config.uae, key):
                    setattr(config.uae, key, value)

        if 'multi_space_vision' in data:
            for key, value in data['multi_space_vision'].items():
                if hasattr(config.multi_space_vision, key):
                    setattr(config.multi_space_vision, key, value)

        if 'neural_mesh' in data:
            for key, value in data['neural_mesh'].items():
                if hasattr(config.neural_mesh, key):
                    setattr(config.neural_mesh, key, value)

        if 'autonomy' in data:
            for key, value in data['autonomy'].items():
                if hasattr(config.autonomy, key):
                    setattr(config.autonomy, key, value)

        # Global settings
        for key in ['debug_mode', 'log_level', 'enable_computer_use',
                    'enable_multi_space', 'enable_voice']:
            if key in data:
                setattr(config, key, data[key])

        return config

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration to dictionary."""
        return {
            'computer_use': {
                'model_name': self.computer_use.model_name,
                'api_timeout': self.computer_use.api_timeout,
                'max_actions_per_task': self.computer_use.max_actions_per_task,
                'action_timeout': self.computer_use.action_timeout,
                'screenshot_max_dimension': self.computer_use.screenshot_max_dimension,
                'capture_timeout': self.computer_use.capture_timeout,
                'thread_pool_workers': self.computer_use.thread_pool_workers,
                'circuit_breaker_threshold': self.computer_use.circuit_breaker_threshold,
                'circuit_breaker_recovery': self.computer_use.circuit_breaker_recovery,
                'enable_narration': self.computer_use.enable_narration,
                'enable_learning': self.computer_use.enable_learning,
            },
            'uae': {
                'monitoring_interval': self.uae.monitoring_interval,
                'context_cache_ttl': self.uae.context_cache_ttl,
                'context_base_weight': self.uae.context_base_weight,
                'situation_base_weight': self.uae.situation_base_weight,
                'recency_threshold': self.uae.recency_threshold,
                'consistency_threshold': self.uae.consistency_threshold,
                'min_confidence': self.uae.min_confidence,
            },
            'multi_space_vision': {
                'capture_all_spaces': self.multi_space_vision.capture_all_spaces,
                'use_yabai': self.multi_space_vision.use_yabai,
                'space_switch_delay': self.multi_space_vision.space_switch_delay,
                'max_spaces_to_capture': self.multi_space_vision.max_spaces_to_capture,
                'enable_window_detection': self.multi_space_vision.enable_window_detection,
            },
            'neural_mesh': {
                'enabled': self.neural_mesh.enabled,
                'message_queue_size': self.neural_mesh.message_queue_size,
                'max_concurrent_agents': self.neural_mesh.max_concurrent_agents,
                'health_check_interval': self.neural_mesh.health_check_interval,
            },
            'autonomy': {
                'default_mode': self.autonomy.default_mode,
                'reasoning_model': self.autonomy.reasoning_model,
                'temperature': self.autonomy.temperature,
                'max_tokens': self.autonomy.max_tokens,
                'max_reasoning_iterations': self.autonomy.max_reasoning_iterations,
                'min_confidence_threshold': self.autonomy.min_confidence_threshold,
                'max_concurrent_tools': self.autonomy.max_concurrent_tools,
                'tool_timeout': self.autonomy.tool_timeout,
                'enable_memory': self.autonomy.enable_memory,
                'working_memory_size': self.autonomy.working_memory_size,
                'max_actions_per_session': self.autonomy.max_actions_per_session,
                'require_permission_high_risk': self.autonomy.require_permission_high_risk,
            },
            'debug_mode': self.debug_mode,
            'log_level': self.log_level,
            'enable_computer_use': self.enable_computer_use,
            'enable_multi_space': self.enable_multi_space,
            'enable_voice': self.enable_voice,
        }

    def save(self, path: Optional[Path] = None) -> None:
        """Save configuration to file."""
        path = path or self.data_dir / "agentic_config.json"
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"Configuration saved to {path}")

    def validate(self) -> List[str]:
        """Validate configuration and return list of issues."""
        issues = []

        # Check API key
        if not self.computer_use.api_key:
            issues.append("ANTHROPIC_API_KEY not set - Computer Use will not work")

        # Check paths
        if not self.data_dir.exists():
            issues.append(f"Data directory does not exist: {self.data_dir}")

        # Check timeouts
        if self.computer_use.api_timeout < self.computer_use.action_timeout:
            issues.append("API timeout should be >= action timeout")

        return issues


# Singleton instance
_config: Optional[AgenticConfig] = None


def get_agentic_config(config_file: Optional[Path] = None) -> AgenticConfig:
    """
    Get the global agentic configuration.

    Args:
        config_file: Optional path to configuration file

    Returns:
        AgenticConfig instance
    """
    global _config

    if _config is None:
        if config_file and config_file.exists():
            _config = AgenticConfig.from_file(config_file)
        else:
            # Check for default config file
            default_path = Path.home() / ".jarvis" / "agentic_config.json"
            if default_path.exists():
                _config = AgenticConfig.from_file(default_path)
            else:
                _config = AgenticConfig()

        # Validate and log issues
        issues = _config.validate()
        for issue in issues:
            logger.warning(f"Config issue: {issue}")

    return _config


def reload_config(config_file: Optional[Path] = None) -> AgenticConfig:
    """Reload configuration from file."""
    global _config
    _config = None
    return get_agentic_config(config_file)

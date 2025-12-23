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
class TwoTierSecurityConfig:
    """Configuration for Two-Tier Agentic Security System."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TWO_TIER_ENABLED", "true"
    ).lower() == "true")

    # Tier 1 settings (safe, low-auth)
    tier1_backend: str = field(default_factory=lambda: os.getenv(
        "JARVIS_TIER1_BACKEND", "gemini"
    ))
    tier1_vbia_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_TIER1_VBIA_THRESHOLD", "0.70"
    )))
    tier1_allow_bypass: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TIER1_ALLOW_BYPASS", "true"
    ).lower() == "true")

    # Tier 2 settings (agentic, strict-auth)
    tier2_backend: str = field(default_factory=lambda: os.getenv(
        "JARVIS_TIER2_BACKEND", "claude"
    ))
    tier2_vbia_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_TIER2_VBIA_THRESHOLD", "0.85"
    )))
    tier2_require_liveness: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TIER2_REQUIRE_LIVENESS", "true"
    ).lower() == "true")

    # Watchdog settings
    watchdog_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_WATCHDOG_ENABLED", "true"
    ).lower() == "true")
    watchdog_heartbeat_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_WATCHDOG_HEARTBEAT_INTERVAL", "5.0"
    )))
    watchdog_heartbeat_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_WATCHDOG_HEARTBEAT_TIMEOUT", "30.0"
    )))
    watchdog_max_actions_per_minute: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_WATCHDOG_MAX_ACTIONS_PM", "30"
    )))

    # Router settings
    router_intent_escalation: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ROUTER_INTENT_ESCALATION", "true"
    ).lower() == "true")
    router_dangerous_command_blocking: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ROUTER_BLOCK_DANGEROUS", "true"
    ).lower() == "true")


@dataclass
class VoiceAuthConfig:
    """Configuration for Voice Biometric Authentication Layer."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_ENABLED", "true"
    ).lower() == "true")
    pre_execution_check: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_PRE_EXECUTION", "true"
    ).lower() == "true")

    # Thresholds
    tier1_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_VOICE_AUTH_TIER1_THRESHOLD", "0.70"
    )))
    tier2_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_VOICE_AUTH_TIER2_THRESHOLD", "0.85"
    )))
    high_risk_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_VOICE_AUTH_HIGH_RISK_THRESHOLD", "0.90"
    )))

    # Cache settings
    cache_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_CACHE", "true"
    ).lower() == "true")
    cache_ttl: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_VOICE_AUTH_CACHE_TTL", "300.0"
    )))

    # Anti-spoofing
    liveness_check_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_LIVENESS", "true"
    ).lower() == "true")
    anti_spoofing_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_ANTI_SPOOF", "true"
    ).lower() == "true")

    # Environmental adaptation
    environmental_adaptation: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_VOICE_AUTH_ENV_ADAPT", "true"
    ).lower() == "true")
    noise_threshold_db: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_VOICE_AUTH_NOISE_THRESHOLD", "-40.0"
    )))


@dataclass
class PhaseManagerConfig:
    """Configuration for LangGraph Phase Manager."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_PHASE_MANAGER_ENABLED", "true"
    ).lower() == "true")

    # Confidence thresholds
    min_analysis_confidence: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_ANALYSIS_CONFIDENCE", "0.70"
    )))
    min_planning_confidence: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_PLANNING_CONFIDENCE", "0.75"
    )))
    min_execution_confidence: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_EXECUTION_CONFIDENCE", "0.80"
    )))

    # Timeouts
    analysis_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_ANALYSIS_TIMEOUT", "30.0"
    )))
    planning_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_PLANNING_TIMEOUT", "60.0"
    )))
    execution_timeout: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_EXECUTION_TIMEOUT", "300.0"
    )))

    # Checkpoints
    checkpoint_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_PHASE_CHECKPOINTS", "true"
    ).lower() == "true")
    max_checkpoints: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_PHASE_MAX_CHECKPOINTS", "10"
    )))

    # Learning
    learning_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_PHASE_LEARNING", "true"
    ).lower() == "true")
    learning_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_PHASE_LEARNING_THRESHOLD", "0.85"
    )))

    # Retries
    max_retries: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_PHASE_MAX_RETRIES", "3"
    )))


@dataclass
class ToolRegistryConfig:
    """Configuration for Unified Tool Registry."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TOOL_REGISTRY_ENABLED", "true"
    ).lower() == "true")

    # Discovery
    auto_discover: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TOOL_AUTO_DISCOVER", "true"
    ).lower() == "true")
    discovery_paths: str = field(default_factory=lambda: os.getenv(
        "JARVIS_TOOL_PATHS", ""
    ))

    # Hot reload
    hot_reload_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TOOL_HOT_RELOAD", "false"
    ).lower() == "true")
    reload_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_TOOL_RELOAD_INTERVAL", "30.0"
    )))

    # Access control
    require_tier2_for_system: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_TIER2_SYSTEM_TOOLS", "true"
    ).lower() == "true")

    # Matching
    match_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_TOOL_MATCH_THRESHOLD", "0.60"
    )))


@dataclass
class MemoryManagerConfig:
    """Configuration for Unified Memory Manager."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_MEMORY_ENABLED", "true"
    ).lower() == "true")

    # Memory limits
    working_memory_max: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_WORKING_MEMORY_MAX", "100"
    )))
    episodic_memory_max: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_EPISODIC_MEMORY_MAX", "1000"
    )))
    semantic_memory_max: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_SEMANTIC_MEMORY_MAX", "500"
    )))

    # Persistence
    persistence_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_MEMORY_PERSIST", "true"
    ).lower() == "true")
    persistence_path: Path = field(default_factory=lambda: Path(os.getenv(
        "JARVIS_MEMORY_PATH", str(Path.home() / ".jarvis" / "memory")
    )))
    auto_save_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_MEMORY_SAVE_INTERVAL", "60.0"
    )))

    # Consolidation
    consolidation_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_MEMORY_CONSOLIDATE", "true"
    ).lower() == "true")
    consolidation_threshold: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_CONSOLIDATION_THRESHOLD", "10"
    )))

    # Experience replay
    replay_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_EXPERIENCE_REPLAY", "true"
    ).lower() == "true")
    replay_similarity: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_REPLAY_SIMILARITY", "0.75"
    )))


@dataclass
class ErrorRecoveryConfig:
    """Configuration for Error Recovery Orchestrator."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ERROR_RECOVERY_ENABLED", "true"
    ).lower() == "true")

    # Retry settings
    max_retries: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_ERROR_MAX_RETRIES", "3"
    )))
    initial_backoff: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_ERROR_INITIAL_BACKOFF", "1.0"
    )))
    max_backoff: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_ERROR_MAX_BACKOFF", "60.0"
    )))
    backoff_multiplier: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_ERROR_BACKOFF_MULTIPLIER", "2.0"
    )))

    # Graceful degradation
    graceful_degradation: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_GRACEFUL_DEGRADATION", "true"
    ).lower() == "true")
    degradation_threshold: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_DEGRADATION_THRESHOLD", "5"
    )))

    # Circuit breaker
    circuit_breaker_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_CIRCUIT_BREAKER", "true"
    ).lower() == "true")
    circuit_threshold: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_CIRCUIT_THRESHOLD", "5"
    )))
    circuit_reset_time: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_CIRCUIT_RESET_TIME", "60.0"
    )))


@dataclass
class UAEContextConfig:
    """Configuration for UAE Context Manager."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_CONTEXT_ENABLED", "true"
    ).lower() == "true")

    # Update settings
    continuous_update: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_CONTINUOUS", "true"
    ).lower() == "true")
    update_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_UAE_UPDATE_INTERVAL", "2.0"
    )))
    history_depth: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_UAE_HISTORY_DEPTH", "10"
    )))

    # Screen capture
    screen_capture_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_SCREEN_CAPTURE", "true"
    ).lower() == "true")
    capture_on_change_only: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_CHANGE_ONLY", "false"
    ).lower() == "true")

    # Change detection
    change_detection_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_CHANGE_DETECT", "true"
    ).lower() == "true")
    change_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_UAE_CHANGE_THRESHOLD", "0.05"
    )))

    # Element tracking
    element_tracking_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_UAE_ELEMENT_TRACK", "true"
    ).lower() == "true")
    max_tracked_elements: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_UAE_MAX_ELEMENTS", "50"
    )))


@dataclass
class InterventionConfig:
    """Configuration for Intervention Orchestrator."""

    # Enable/disable
    enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_INTERVENTION_ENABLED", "true"
    ).lower() == "true")

    # Timing
    min_interval: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_INTERVENTION_MIN_INTERVAL", "10.0"
    )))
    max_queue_size: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_INTERVENTION_MAX_QUEUE", "5"
    )))

    # Optimal timing
    optimal_timing_enabled: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_INTERVENTION_TIMING", "true"
    ).lower() == "true")
    idle_detection_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_IDLE_THRESHOLD", "5.0"
    )))

    # Learning
    learn_effectiveness: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_INTERVENTION_LEARN", "true"
    ).lower() == "true")
    min_learning_samples: int = field(default_factory=lambda: int(os.getenv(
        "JARVIS_LEARNING_SAMPLES", "10"
    )))

    # Priority
    urgent_threshold: float = field(default_factory=lambda: float(os.getenv(
        "JARVIS_URGENT_PRIORITY", "0.9"
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
    Supports 60+ AI agents with unified configuration management.
    """

    # Core subsystem configurations
    computer_use: ComputerUseConfig = field(default_factory=ComputerUseConfig)
    uae: UAEConfig = field(default_factory=UAEConfig)
    multi_space_vision: MultiSpaceVisionConfig = field(default_factory=MultiSpaceVisionConfig)
    neural_mesh: NeuralMeshConfig = field(default_factory=NeuralMeshConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)

    # Two-Tier Agentic Security subsystem
    two_tier_security: TwoTierSecurityConfig = field(default_factory=TwoTierSecurityConfig)
    voice_auth: VoiceAuthConfig = field(default_factory=VoiceAuthConfig)

    # LangGraph and Execution subsystems
    phase_manager: PhaseManagerConfig = field(default_factory=PhaseManagerConfig)
    tool_registry: ToolRegistryConfig = field(default_factory=ToolRegistryConfig)
    memory_manager: MemoryManagerConfig = field(default_factory=MemoryManagerConfig)
    error_recovery: ErrorRecoveryConfig = field(default_factory=ErrorRecoveryConfig)

    # UAE and Intervention subsystems
    uae_context: UAEContextConfig = field(default_factory=UAEContextConfig)
    intervention: InterventionConfig = field(default_factory=InterventionConfig)

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
    enable_two_tier_security: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_ENABLE_TWO_TIER", "true"
    ).lower() == "true")
    enable_neural_mesh_deep: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_NEURAL_MESH_DEEP", "true"
    ).lower() == "true")
    enable_agi_os_events: bool = field(default_factory=lambda: os.getenv(
        "JARVIS_AGI_OS_EVENTS", "true"
    ).lower() == "true")

    def __post_init__(self):
        """Ensure directories exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.computer_use.learned_positions_path.parent.mkdir(parents=True, exist_ok=True)
        self.uae.knowledge_base_path.parent.mkdir(parents=True, exist_ok=True)
        # Ensure memory persistence path exists
        if self.memory_manager.persistence_enabled:
            self.memory_manager.persistence_path.mkdir(parents=True, exist_ok=True)

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

        # Helper to map nested config attributes
        def _map_nested(key: str, target: Any) -> None:
            if key in data:
                for attr, value in data[key].items():
                    if hasattr(target, attr):
                        setattr(target, attr, value)

        # Map core subsystem configs
        _map_nested('computer_use', config.computer_use)
        _map_nested('uae', config.uae)
        _map_nested('multi_space_vision', config.multi_space_vision)
        _map_nested('neural_mesh', config.neural_mesh)
        _map_nested('autonomy', config.autonomy)

        # Map Two-Tier Security subsystems
        _map_nested('two_tier_security', config.two_tier_security)
        _map_nested('voice_auth', config.voice_auth)

        # Map LangGraph and Execution subsystems
        _map_nested('phase_manager', config.phase_manager)
        _map_nested('tool_registry', config.tool_registry)
        _map_nested('memory_manager', config.memory_manager)
        _map_nested('error_recovery', config.error_recovery)

        # Map UAE and Intervention subsystems
        _map_nested('uae_context', config.uae_context)
        _map_nested('intervention', config.intervention)

        # Global settings
        global_keys = [
            'debug_mode', 'log_level', 'enable_computer_use',
            'enable_multi_space', 'enable_voice', 'enable_two_tier_security',
            'enable_neural_mesh_deep', 'enable_agi_os_events'
        ]
        for key in global_keys:
            if key in data:
                setattr(config, key, data[key])

        return config

    def to_dict(self) -> Dict[str, Any]:
        """Export configuration to dictionary."""
        return {
            # Core subsystems
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

            # Two-Tier Security subsystems
            'two_tier_security': {
                'enabled': self.two_tier_security.enabled,
                'tier1_backend': self.two_tier_security.tier1_backend,
                'tier1_vbia_threshold': self.two_tier_security.tier1_vbia_threshold,
                'tier1_allow_bypass': self.two_tier_security.tier1_allow_bypass,
                'tier2_backend': self.two_tier_security.tier2_backend,
                'tier2_vbia_threshold': self.two_tier_security.tier2_vbia_threshold,
                'tier2_require_liveness': self.two_tier_security.tier2_require_liveness,
                'watchdog_enabled': self.two_tier_security.watchdog_enabled,
                'watchdog_heartbeat_interval': self.two_tier_security.watchdog_heartbeat_interval,
                'watchdog_heartbeat_timeout': self.two_tier_security.watchdog_heartbeat_timeout,
                'watchdog_max_actions_per_minute': self.two_tier_security.watchdog_max_actions_per_minute,
                'router_intent_escalation': self.two_tier_security.router_intent_escalation,
                'router_dangerous_command_blocking': self.two_tier_security.router_dangerous_command_blocking,
            },
            'voice_auth': {
                'enabled': self.voice_auth.enabled,
                'pre_execution_check': self.voice_auth.pre_execution_check,
                'tier1_threshold': self.voice_auth.tier1_threshold,
                'tier2_threshold': self.voice_auth.tier2_threshold,
                'high_risk_threshold': self.voice_auth.high_risk_threshold,
                'cache_enabled': self.voice_auth.cache_enabled,
                'cache_ttl': self.voice_auth.cache_ttl,
                'liveness_check_enabled': self.voice_auth.liveness_check_enabled,
                'anti_spoofing_enabled': self.voice_auth.anti_spoofing_enabled,
                'environmental_adaptation': self.voice_auth.environmental_adaptation,
                'noise_threshold_db': self.voice_auth.noise_threshold_db,
            },

            # LangGraph and Execution subsystems
            'phase_manager': {
                'enabled': self.phase_manager.enabled,
                'min_analysis_confidence': self.phase_manager.min_analysis_confidence,
                'min_planning_confidence': self.phase_manager.min_planning_confidence,
                'min_execution_confidence': self.phase_manager.min_execution_confidence,
                'analysis_timeout': self.phase_manager.analysis_timeout,
                'planning_timeout': self.phase_manager.planning_timeout,
                'execution_timeout': self.phase_manager.execution_timeout,
                'checkpoint_enabled': self.phase_manager.checkpoint_enabled,
                'max_checkpoints': self.phase_manager.max_checkpoints,
                'learning_enabled': self.phase_manager.learning_enabled,
                'learning_threshold': self.phase_manager.learning_threshold,
                'max_retries': self.phase_manager.max_retries,
            },
            'tool_registry': {
                'enabled': self.tool_registry.enabled,
                'auto_discover': self.tool_registry.auto_discover,
                'discovery_paths': self.tool_registry.discovery_paths,
                'hot_reload_enabled': self.tool_registry.hot_reload_enabled,
                'reload_interval': self.tool_registry.reload_interval,
                'require_tier2_for_system': self.tool_registry.require_tier2_for_system,
                'match_threshold': self.tool_registry.match_threshold,
            },
            'memory_manager': {
                'enabled': self.memory_manager.enabled,
                'working_memory_max': self.memory_manager.working_memory_max,
                'episodic_memory_max': self.memory_manager.episodic_memory_max,
                'semantic_memory_max': self.memory_manager.semantic_memory_max,
                'persistence_enabled': self.memory_manager.persistence_enabled,
                'persistence_path': str(self.memory_manager.persistence_path),
                'auto_save_interval': self.memory_manager.auto_save_interval,
                'consolidation_enabled': self.memory_manager.consolidation_enabled,
                'consolidation_threshold': self.memory_manager.consolidation_threshold,
                'replay_enabled': self.memory_manager.replay_enabled,
                'replay_similarity': self.memory_manager.replay_similarity,
            },
            'error_recovery': {
                'enabled': self.error_recovery.enabled,
                'max_retries': self.error_recovery.max_retries,
                'initial_backoff': self.error_recovery.initial_backoff,
                'max_backoff': self.error_recovery.max_backoff,
                'backoff_multiplier': self.error_recovery.backoff_multiplier,
                'graceful_degradation': self.error_recovery.graceful_degradation,
                'degradation_threshold': self.error_recovery.degradation_threshold,
                'circuit_breaker_enabled': self.error_recovery.circuit_breaker_enabled,
                'circuit_threshold': self.error_recovery.circuit_threshold,
                'circuit_reset_time': self.error_recovery.circuit_reset_time,
            },

            # UAE and Intervention subsystems
            'uae_context': {
                'enabled': self.uae_context.enabled,
                'continuous_update': self.uae_context.continuous_update,
                'update_interval': self.uae_context.update_interval,
                'history_depth': self.uae_context.history_depth,
                'screen_capture_enabled': self.uae_context.screen_capture_enabled,
                'capture_on_change_only': self.uae_context.capture_on_change_only,
                'change_detection_enabled': self.uae_context.change_detection_enabled,
                'change_threshold': self.uae_context.change_threshold,
                'element_tracking_enabled': self.uae_context.element_tracking_enabled,
                'max_tracked_elements': self.uae_context.max_tracked_elements,
            },
            'intervention': {
                'enabled': self.intervention.enabled,
                'min_interval': self.intervention.min_interval,
                'max_queue_size': self.intervention.max_queue_size,
                'optimal_timing_enabled': self.intervention.optimal_timing_enabled,
                'idle_detection_threshold': self.intervention.idle_detection_threshold,
                'learn_effectiveness': self.intervention.learn_effectiveness,
                'min_learning_samples': self.intervention.min_learning_samples,
                'urgent_threshold': self.intervention.urgent_threshold,
            },

            # Global settings
            'debug_mode': self.debug_mode,
            'log_level': self.log_level,
            'enable_computer_use': self.enable_computer_use,
            'enable_multi_space': self.enable_multi_space,
            'enable_voice': self.enable_voice,
            'enable_two_tier_security': self.enable_two_tier_security,
            'enable_neural_mesh_deep': self.enable_neural_mesh_deep,
            'enable_agi_os_events': self.enable_agi_os_events,
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

        # === Core Subsystem Validation ===

        # Check API key
        if not self.computer_use.api_key:
            issues.append("ANTHROPIC_API_KEY not set - Computer Use will not work")

        # Check paths
        if not self.data_dir.exists():
            issues.append(f"Data directory does not exist: {self.data_dir}")

        # Check timeouts
        if self.computer_use.api_timeout < self.computer_use.action_timeout:
            issues.append("API timeout should be >= action timeout")

        # === Two-Tier Security Validation ===

        if self.enable_two_tier_security and self.two_tier_security.enabled:
            # Validate VBIA thresholds
            if not (0.0 <= self.two_tier_security.tier1_vbia_threshold <= 1.0):
                issues.append("Tier 1 VBIA threshold must be between 0.0 and 1.0")
            if not (0.0 <= self.two_tier_security.tier2_vbia_threshold <= 1.0):
                issues.append("Tier 2 VBIA threshold must be between 0.0 and 1.0")
            if self.two_tier_security.tier1_vbia_threshold > self.two_tier_security.tier2_vbia_threshold:
                issues.append("Tier 1 VBIA threshold should be <= Tier 2 threshold")

            # Validate watchdog settings
            if self.two_tier_security.watchdog_heartbeat_timeout <= self.two_tier_security.watchdog_heartbeat_interval:
                issues.append("Watchdog timeout must be > heartbeat interval")
            if self.two_tier_security.watchdog_max_actions_per_minute < 1:
                issues.append("Watchdog max actions per minute must be >= 1")

        # === Voice Auth Validation ===

        if self.voice_auth.enabled:
            if not (0.0 <= self.voice_auth.tier1_threshold <= 1.0):
                issues.append("Voice auth tier1 threshold must be between 0.0 and 1.0")
            if not (0.0 <= self.voice_auth.tier2_threshold <= 1.0):
                issues.append("Voice auth tier2 threshold must be between 0.0 and 1.0")
            if not (0.0 <= self.voice_auth.high_risk_threshold <= 1.0):
                issues.append("Voice auth high risk threshold must be between 0.0 and 1.0")
            if self.voice_auth.cache_ttl < 0:
                issues.append("Voice auth cache TTL must be >= 0")

        # === Phase Manager Validation ===

        if self.phase_manager.enabled:
            # Validate confidence thresholds
            for name, threshold in [
                ("analysis", self.phase_manager.min_analysis_confidence),
                ("planning", self.phase_manager.min_planning_confidence),
                ("execution", self.phase_manager.min_execution_confidence),
            ]:
                if not (0.0 <= threshold <= 1.0):
                    issues.append(f"Phase manager {name} confidence must be between 0.0 and 1.0")

            # Validate timeouts
            if self.phase_manager.analysis_timeout <= 0:
                issues.append("Phase manager analysis timeout must be > 0")
            if self.phase_manager.max_retries < 0:
                issues.append("Phase manager max retries must be >= 0")

        # === Memory Manager Validation ===

        if self.memory_manager.enabled:
            if self.memory_manager.working_memory_max < 1:
                issues.append("Working memory max must be >= 1")
            if self.memory_manager.episodic_memory_max < 1:
                issues.append("Episodic memory max must be >= 1")
            if self.memory_manager.auto_save_interval < 0:
                issues.append("Memory auto save interval must be >= 0")
            if not (0.0 <= self.memory_manager.replay_similarity <= 1.0):
                issues.append("Memory replay similarity must be between 0.0 and 1.0")

        # === Error Recovery Validation ===

        if self.error_recovery.enabled:
            if self.error_recovery.max_retries < 0:
                issues.append("Error recovery max retries must be >= 0")
            if self.error_recovery.initial_backoff <= 0:
                issues.append("Error recovery initial backoff must be > 0")
            if self.error_recovery.max_backoff < self.error_recovery.initial_backoff:
                issues.append("Error recovery max backoff must be >= initial backoff")
            if self.error_recovery.backoff_multiplier < 1.0:
                issues.append("Error recovery backoff multiplier must be >= 1.0")

        # === UAE Context Validation ===

        if self.uae_context.enabled:
            if self.uae_context.update_interval <= 0:
                issues.append("UAE context update interval must be > 0")
            if self.uae_context.history_depth < 1:
                issues.append("UAE context history depth must be >= 1")
            if not (0.0 <= self.uae_context.change_threshold <= 1.0):
                issues.append("UAE context change threshold must be between 0.0 and 1.0")

        # === Intervention Validation ===

        if self.intervention.enabled:
            if self.intervention.min_interval <= 0:
                issues.append("Intervention min interval must be > 0")
            if self.intervention.max_queue_size < 1:
                issues.append("Intervention max queue size must be >= 1")
            if not (0.0 <= self.intervention.urgent_threshold <= 1.0):
                issues.append("Intervention urgent threshold must be between 0.0 and 1.0")

        # === Tool Registry Validation ===

        if self.tool_registry.enabled:
            if not (0.0 <= self.tool_registry.match_threshold <= 1.0):
                issues.append("Tool registry match threshold must be between 0.0 and 1.0")
            if self.tool_registry.hot_reload_enabled and self.tool_registry.reload_interval <= 0:
                issues.append("Tool registry reload interval must be > 0 when hot reload enabled")

        # === Neural Mesh Validation ===

        if self.neural_mesh.enabled:
            if self.neural_mesh.message_queue_size < 1:
                issues.append("Neural mesh message queue size must be >= 1")
            if self.neural_mesh.max_concurrent_agents < 1:
                issues.append("Neural mesh max concurrent agents must be >= 1")

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

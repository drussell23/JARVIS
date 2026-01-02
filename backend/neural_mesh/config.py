"""
JARVIS Neural Mesh - Configuration System

Centralized configuration for all Neural Mesh components.
Supports YAML files, environment variables, and programmatic configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class CommunicationBusConfig:
    """Configuration for the Agent Communication Bus."""

    # Queue sizes for each priority level
    queue_sizes: Dict[int, int] = field(default_factory=lambda: {
        0: 1000,   # CRITICAL
        1: 5000,   # HIGH
        2: 10000,  # NORMAL
        3: 20000,  # LOW
    })

    # Message history size for debugging
    message_history_size: int = 1000

    # Latency targets in milliseconds
    latency_targets_ms: Dict[int, float] = field(default_factory=lambda: {
        0: 1.0,    # CRITICAL: <1ms
        1: 5.0,    # HIGH: <5ms
        2: 10.0,   # NORMAL: <10ms
        3: 50.0,   # LOW: <50ms
    })

    # Message expiration defaults (seconds)
    default_ttl_seconds: float = 300.0

    # Processing settings
    max_concurrent_handlers: int = 100
    handler_timeout_seconds: float = 10.0

    # Persistence
    persist_messages: bool = True
    persistence_path: str = ""

    # Metrics
    enable_metrics: bool = True
    metrics_interval_seconds: float = 60.0


@dataclass
class KnowledgeGraphConfig:
    """Configuration for the Shared Knowledge Graph."""

    # Vector database settings
    embedding_dimension: int = 384
    embedding_model: str = "all-MiniLM-L6-v2"
    similarity_threshold: float = 0.7

    # ChromaDB settings
    chroma_persist_directory: str = ""
    chroma_collection_name: str = "jarvis_knowledge"

    # Graph settings
    max_entries: int = 100000
    default_ttl_seconds: float = 86400.0  # 24 hours

    # Cache settings
    cache_size: int = 1000
    cache_ttl_seconds: float = 300.0

    # Query settings
    default_query_limit: int = 10
    max_query_limit: int = 100

    # Persistence
    persist_graph: bool = True
    graph_persist_path: str = ""

    # Background tasks
    cleanup_interval_seconds: float = 3600.0  # 1 hour
    sync_interval_seconds: float = 60.0


@dataclass
class AgentRegistryConfig:
    """Configuration for the Agent Registry."""

    # Heartbeat settings
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 30.0
    health_check_interval_seconds: float = 5.0

    # Agent limits
    max_agents: int = 200
    max_capabilities_per_agent: int = 50

    # Load balancing
    enable_load_balancing: bool = True
    load_threshold: float = 0.8

    # Persistence
    persist_registry: bool = True
    registry_path: str = ""

    # Metrics
    enable_metrics: bool = True


@dataclass
class OrchestratorConfig:
    """Configuration for the Multi-Agent Orchestrator."""

    # Task settings
    default_task_timeout_seconds: float = 30.0
    max_task_timeout_seconds: float = 300.0
    default_retry_count: int = 3
    retry_delay_seconds: float = 1.0
    retry_backoff_multiplier: float = 2.0

    # Workflow settings
    max_concurrent_workflows: int = 50
    max_tasks_per_workflow: int = 100
    workflow_timeout_seconds: float = 600.0  # 10 minutes

    # Execution strategies
    default_execution_strategy: str = "hybrid"

    # Resource management
    enable_resource_pooling: bool = True
    max_parallel_tasks: int = 20


@dataclass
class BaseAgentConfig:
    """Configuration for Base Agent behavior."""

    # Heartbeat
    heartbeat_interval_seconds: float = 10.0

    # Message handling
    message_queue_size: int = 1000
    message_handler_timeout_seconds: float = 10.0

    # Knowledge
    enable_knowledge_access: bool = True
    knowledge_cache_size: int = 100

    # Logging
    log_messages: bool = True
    log_level: str = "INFO"


@dataclass
class NeuralMeshConfig:
    """Master configuration for the entire Neural Mesh system."""

    # Identity - used by integration.py for coordinator naming
    name: str = "JARVIS-Neural-Mesh"

    # Feature flags - used by integration.py for selective initialization
    enable_monitoring: bool = True
    enable_knowledge_graph: bool = True
    enable_communication_bus: bool = True

    # Component configs
    communication_bus: CommunicationBusConfig = field(default_factory=CommunicationBusConfig)
    knowledge_graph: KnowledgeGraphConfig = field(default_factory=KnowledgeGraphConfig)
    agent_registry: AgentRegistryConfig = field(default_factory=AgentRegistryConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    base_agent: BaseAgentConfig = field(default_factory=BaseAgentConfig)

    # Global settings
    data_directory: str = ""
    log_directory: str = ""
    enable_telemetry: bool = True
    debug_mode: bool = False

    # Memory management
    max_memory_mb: int = 500
    enable_memory_monitoring: bool = True

    # Integration
    enable_learning_db_integration: bool = True
    enable_chromadb_integration: bool = True

    def __post_init__(self) -> None:
        """Initialize paths and validate configuration."""
        # Set default data directory
        if not self.data_directory:
            jarvis_home = os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis"))
            self.data_directory = str(Path(jarvis_home) / "neural_mesh")

        # Set default log directory
        if not self.log_directory:
            self.log_directory = str(Path(self.data_directory) / "logs")

        # Set component paths
        if not self.communication_bus.persistence_path:
            self.communication_bus.persistence_path = str(
                Path(self.data_directory) / "messages"
            )

        if not self.knowledge_graph.chroma_persist_directory:
            self.knowledge_graph.chroma_persist_directory = str(
                Path(self.data_directory) / "vector_db"
            )

        if not self.knowledge_graph.graph_persist_path:
            self.knowledge_graph.graph_persist_path = str(
                Path(self.data_directory) / "graph_db"
            )

        if not self.agent_registry.registry_path:
            self.agent_registry.registry_path = str(
                Path(self.data_directory) / "registry"
            )

    @classmethod
    def from_yaml(cls, yaml_path: str) -> "NeuralMeshConfig":
        """Load configuration from a YAML file."""
        with open(yaml_path, "r") as f:
            data = yaml.safe_load(f)

        return cls.from_dict(data or {})

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NeuralMeshConfig":
        """Create configuration from a dictionary."""
        config = cls()

        # Update identity and feature flags
        if "name" in data:
            config.name = data["name"]
        if "enable_monitoring" in data:
            config.enable_monitoring = data["enable_monitoring"]
        if "enable_knowledge_graph" in data:
            config.enable_knowledge_graph = data["enable_knowledge_graph"]
        if "enable_communication_bus" in data:
            config.enable_communication_bus = data["enable_communication_bus"]

        # Update global settings
        if "data_directory" in data:
            config.data_directory = data["data_directory"]
        if "log_directory" in data:
            config.log_directory = data["log_directory"]
        if "enable_telemetry" in data:
            config.enable_telemetry = data["enable_telemetry"]
        if "debug_mode" in data:
            config.debug_mode = data["debug_mode"]
        if "max_memory_mb" in data:
            config.max_memory_mb = data["max_memory_mb"]

        # Update component configs
        if "communication_bus" in data:
            for key, value in data["communication_bus"].items():
                if hasattr(config.communication_bus, key):
                    setattr(config.communication_bus, key, value)

        if "knowledge_graph" in data:
            for key, value in data["knowledge_graph"].items():
                if hasattr(config.knowledge_graph, key):
                    setattr(config.knowledge_graph, key, value)

        if "agent_registry" in data:
            for key, value in data["agent_registry"].items():
                if hasattr(config.agent_registry, key):
                    setattr(config.agent_registry, key, value)

        if "orchestrator" in data:
            for key, value in data["orchestrator"].items():
                if hasattr(config.orchestrator, key):
                    setattr(config.orchestrator, key, value)

        if "base_agent" in data:
            for key, value in data["base_agent"].items():
                if hasattr(config.base_agent, key):
                    setattr(config.base_agent, key, value)

        # Re-run post_init to set paths
        config.__post_init__()

        return config

    @classmethod
    def from_env(cls) -> "NeuralMeshConfig":
        """Create configuration from environment variables."""
        config = cls()

        # Map environment variables to config
        env_mapping = {
            "NEURAL_MESH_DATA_DIR": "data_directory",
            "NEURAL_MESH_LOG_DIR": "log_directory",
            "NEURAL_MESH_DEBUG": "debug_mode",
            "NEURAL_MESH_MAX_MEMORY_MB": "max_memory_mb",
        }

        for env_var, config_key in env_mapping.items():
            value = os.environ.get(env_var)
            if value is not None:
                if config_key == "debug_mode":
                    value = value.lower() in ("true", "1", "yes")
                elif config_key == "max_memory_mb":
                    value = int(value)
                setattr(config, config_key, value)

        config.__post_init__()
        return config

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "data_directory": self.data_directory,
            "log_directory": self.log_directory,
            "enable_telemetry": self.enable_telemetry,
            "debug_mode": self.debug_mode,
            "max_memory_mb": self.max_memory_mb,
            "communication_bus": {
                k: v for k, v in self.communication_bus.__dict__.items()
            },
            "knowledge_graph": {
                k: v for k, v in self.knowledge_graph.__dict__.items()
            },
            "agent_registry": {
                k: v for k, v in self.agent_registry.__dict__.items()
            },
            "orchestrator": {
                k: v for k, v in self.orchestrator.__dict__.items()
            },
            "base_agent": {
                k: v for k, v in self.base_agent.__dict__.items()
            },
        }

    def save_yaml(self, yaml_path: str) -> None:
        """Save configuration to a YAML file."""
        Path(yaml_path).parent.mkdir(parents=True, exist_ok=True)
        with open(yaml_path, "w") as f:
            yaml.dump(self.to_dict(), f, default_flow_style=False, sort_keys=False)

    def ensure_directories(self) -> None:
        """Create all required directories."""
        directories = [
            self.data_directory,
            self.log_directory,
            self.communication_bus.persistence_path,
            self.knowledge_graph.chroma_persist_directory,
            self.knowledge_graph.graph_persist_path,
            self.agent_registry.registry_path,
        ]

        for directory in directories:
            if directory:
                Path(directory).mkdir(parents=True, exist_ok=True)


# Global configuration instance (lazy loaded)
_global_config: Optional[NeuralMeshConfig] = None


def get_config() -> NeuralMeshConfig:
    """Get the global Neural Mesh configuration."""
    global _global_config
    if _global_config is None:
        # Try to load from default locations
        config_paths = [
            Path.cwd() / "config" / "neural_mesh.yaml",
            Path.home() / ".jarvis" / "config" / "neural_mesh.yaml",
            Path(__file__).parent.parent.parent / "config" / "neural_mesh.yaml",
        ]

        for path in config_paths:
            if path.exists():
                _global_config = NeuralMeshConfig.from_yaml(str(path))
                break

        if _global_config is None:
            # Use defaults with environment overrides
            _global_config = NeuralMeshConfig.from_env()

    return _global_config


def set_config(config: NeuralMeshConfig) -> None:
    """Set the global Neural Mesh configuration."""
    global _global_config
    _global_config = config

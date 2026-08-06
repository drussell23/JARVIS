"""
Autonomous Reasoning Configuration
===================================

Configuration for LangGraph + LangChain integration with JARVIS.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from pathlib import Path

@dataclass
class AutonomousConfig:
    """Configuration for autonomous reasoning system"""
    
    # Model Configuration
    reasoning_model: str = "claude-opus-4-20250514"  # Primary reasoning
    fast_model: str = "claude-3-5-sonnet-20241022"   # Fast operations
    fallback_model: str = "gpt-4o-2024-08-06"        # Fallback
    
    # LangSmith Configuration (Observability)
    langsmith_enabled: bool = True
    langsmith_project: str = "jarvis-autonomous"
    langsmith_api_key: Optional[str] = field(
        default_factory=lambda: os.getenv("LANGCHAIN_API_KEY")
    )
    
    # Langfuse Configuration (Monitoring)
    langfuse_enabled: bool = True
    langfuse_public_key: Optional[str] = field(
        default_factory=lambda: os.getenv("LANGFUSE_PUBLIC_KEY")
    )
    langfuse_secret_key: Optional[str] = field(
        default_factory=lambda: os.getenv("LANGFUSE_SECRET_KEY")
    )
    langfuse_host: str = "https://cloud.langfuse.com"
    
    # Reasoning Configuration
    max_reasoning_steps: int = 50
    max_retries: int = 3
    reasoning_timeout_seconds: int = 300  # 5 minutes
    enable_self_reflection: bool = True
    
    # Tool Configuration
    max_parallel_tools: int = 3
    tool_timeout_seconds: int = 60
    enable_tool_caching: bool = True
    
    # Memory Configuration
    conversation_memory_size: int = 10  # Last N turns
    enable_semantic_memory: bool = True  # Requires ChromaDB
    semantic_memory_top_k: int = 5
    
    # Safety Configuration
    require_confirmation_for: List[str] = field(default_factory=lambda: [
        "file_deletion",
        "system_modification",
        "external_api_calls",
        "code_execution"
    ])
    enable_safety_guardrails: bool = True
    max_cost_per_task_usd: float = 1.0
    
    # State Management
    state_persistence_enabled: bool = True
    state_storage_path: Path = Path("backend/database/agent_state")
    use_redis_for_state: bool = False
    redis_url: Optional[str] = field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379")
    )
    
    # Performance
    enable_streaming: bool = True
    batch_tool_calls: bool = True
    cache_tool_results: bool = True
    
    # Logging
    log_level: str = "INFO"
    log_reasoning_steps: bool = True
    log_tool_usage: bool = True
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary"""
        return {
            "reasoning_model": self.reasoning_model,
            "fast_model": self.fast_model,
            "fallback_model": self.fallback_model,
            "max_reasoning_steps": self.max_reasoning_steps,
            "max_retries": self.max_retries,
            "enable_self_reflection": self.enable_self_reflection,
            "enable_safety_guardrails": self.enable_safety_guardrails,
        }
    
    @classmethod
    def from_env(cls) -> "AutonomousConfig":
        """Create configuration from environment variables"""
        return cls(
            reasoning_model=os.getenv("JARVIS_REASONING_MODEL", "claude-opus-4-20250514"),
            langsmith_enabled=os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true",
            langfuse_enabled=os.getenv("LANGFUSE_ENABLED", "true").lower() == "true",
        )


# Global configuration instance
_global_config: Optional[AutonomousConfig] = None


def get_autonomous_config() -> AutonomousConfig:
    """Get or create global autonomous configuration"""
    global _global_config
    if _global_config is None:
        _global_config = AutonomousConfig.from_env()
    return _global_config


def set_autonomous_config(config: AutonomousConfig):
    """Set global autonomous configuration"""
    global _global_config
    _global_config = config

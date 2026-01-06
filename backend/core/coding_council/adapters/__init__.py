"""
v77.3: Coding Council Framework Adapters (Anthropic-Powered)
============================================================

Adapters for integrating coding frameworks with the Coding Council.

Primary: Anthropic Claude API (no external dependencies)
Fallback: External tools (Aider CLI, MetaGPT package, etc.)

Each adapter provides a unified interface for:
- analyze(): Codebase analysis
- plan(): Task planning
- execute(): Code modification

Adapters:
    AnthropicUnifiedEngine - Primary Claude-powered engine (recommended)
    AiderAdapter     - Aider-style editing (uses Claude, falls back to CLI)
    OpenHandsAdapter - Sandboxed execution in Docker
    MetaGPTAdapter   - Multi-agent planning (uses Claude, falls back to package)
    RepoMasterAdapter - Codebase analysis
    ContinueAdapter   - IDE integration

Author: JARVIS v77.3
Version: 2.0.0
"""

from __future__ import annotations

__all__ = [
    "AnthropicUnifiedEngine",
    "get_anthropic_engine",
    "AiderAdapter",
    "OpenHandsAdapter",
    "MetaGPTAdapter",
    "RepoMasterAdapter",
    "ContinueAdapter",
]


def __getattr__(name: str):
    """Lazy import adapters to avoid heavy dependencies."""
    if name == "AnthropicUnifiedEngine":
        from .anthropic_engine import AnthropicUnifiedEngine
        return AnthropicUnifiedEngine
    elif name == "get_anthropic_engine":
        from .anthropic_engine import get_anthropic_engine
        return get_anthropic_engine
    elif name == "AiderAdapter":
        from .aider_adapter import AiderAdapter
        return AiderAdapter
    elif name == "OpenHandsAdapter":
        from .openhands_adapter import OpenHandsAdapter
        return OpenHandsAdapter
    elif name == "MetaGPTAdapter":
        from .metagpt_adapter import MetaGPTAdapter
        return MetaGPTAdapter
    elif name == "RepoMasterAdapter":
        from .repomaster_adapter import RepoMasterAdapter
        return RepoMasterAdapter
    elif name == "ContinueAdapter":
        from .continue_adapter import ContinueAdapter
        return ContinueAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

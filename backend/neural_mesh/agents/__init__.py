"""
JARVIS Neural Mesh - Production Agents

Expose agent classes and initializer helpers without eagerly importing the
entire agent fleet. This keeps focused imports like
`backend.neural_mesh.agents.google_workspace_agent` from pulling in unrelated
modules such as visual monitoring.
"""

from __future__ import annotations

from importlib import import_module
from typing import Dict, Tuple

_EXPORTS: Dict[str, Tuple[str, str]] = {
    "MemoryAgent": (".memory_agent", "MemoryAgent"),
    "CoordinatorAgent": (".coordinator_agent", "CoordinatorAgent"),
    "HealthMonitorAgent": (".health_monitor_agent", "HealthMonitorAgent"),
    "ContextTrackerAgent": (".context_tracker_agent", "ContextTrackerAgent"),
    "ErrorAnalyzerAgent": (".error_analyzer_agent", "ErrorAnalyzerAgent"),
    "PatternRecognitionAgent": (".pattern_recognition_agent", "PatternRecognitionAgent"),
    "VisualMonitorAgent": (".visual_monitor_agent", "VisualMonitorAgent"),
    "WebSearchAgent": (".web_search_agent", "WebSearchAgent"),
    "AgentInitializer": (".agent_initializer", "AgentInitializer"),
    "PRODUCTION_AGENTS": (".agent_initializer", "PRODUCTION_AGENTS"),
    "get_agent_initializer": (".agent_initializer", "get_agent_initializer"),
    "initialize_production_agents": (".agent_initializer", "initialize_production_agents"),
    "shutdown_production_agents": (".agent_initializer", "shutdown_production_agents"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    """Import agent exports on demand to avoid package-wide side effects."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value

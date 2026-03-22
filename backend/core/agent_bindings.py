"""Agent Bindings — single declarative source for agent ID → executor mapping.

Both Zone 6.12 (AgentRegistry metadata) and RuntimeTaskOrchestrator (live dispatch)
read from the same manifest.  The default manifest ships in-repo; user overrides
merge from ``~/.jarvis/agent_bindings.json`` or ``JARVIS_AGENT_BINDINGS_PATH``.

Each entry:
    {
        "agent_id":      "visual_browser_agent",
        "module":        "backend.neural_mesh.agents.visual_browser_agent",
        "class_name":    "VisualBrowserAgent",
        "agent_type":    "vision",
        "capabilities":  ["visual_browser", "web_browsing", "browser", "browse_and_interact"],
        "backend":       "local"
    }
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AgentBinding:
    """One agent's binding: identity, import spec, and capabilities."""
    agent_id: str
    module: str
    class_name: str
    agent_type: str
    capabilities: Set[str]
    backend: str = "local"

    def instantiate(self) -> Any:
        """Import the module and create an instance of class_name."""
        mod = importlib.import_module(self.module)
        cls = getattr(mod, self.class_name)
        return cls()


# ---------------------------------------------------------------------------
# Default manifest (ships with repo — single source of truth)
# ---------------------------------------------------------------------------

_DEFAULT_BINDINGS: List[Dict[str, Any]] = [
    {
        "agent_id": "visual_browser_agent",
        "module": "backend.neural_mesh.agents.visual_browser_agent",
        "class_name": "VisualBrowserAgent",
        "agent_type": "vision",
        "capabilities": ["visual_browser", "web_browsing", "browser", "browse_and_interact"],
    },
    {
        "agent_id": "web_search_agent",
        "module": "backend.neural_mesh.agents.web_search_agent",
        "class_name": "WebSearchAgent",
        "agent_type": "intelligence",
        "capabilities": ["web_search", "web_research", "read_web_page", "search"],
    },
    {
        "agent_id": "native_app_control_agent",
        "module": "backend.neural_mesh.agents.native_app_control_agent",
        "class_name": "NativeAppControlAgent",
        "agent_type": "control",
        "capabilities": ["native_app_control", "interact_with_app", "app_control", "open_app"],
    },
    {
        "agent_id": "google_workspace_agent",
        "module": "backend.neural_mesh.agents.google_workspace_agent",
        "class_name": "GoogleWorkspaceAgent",
        "agent_type": "workspace",
        "capabilities": ["handle_workspace_query", "read_email", "check_calendar",
                         "send_email", "create_event", "email", "calendar"],
    },
    {
        "agent_id": "spatial_awareness_agent",
        "module": "backend.neural_mesh.agents.spatial_awareness_agent",
        "class_name": "SpatialAwarenessAgent",
        "agent_type": "awareness",
        "capabilities": ["get_spatial_context", "switch_space", "get_active_app"],
    },
    {
        "agent_id": "predictive_planning_agent",
        "module": "backend.neural_mesh.agents.predictive_planning_agent",
        "class_name": "PredictivePlanningAgent",
        "agent_type": "intelligence",
        "capabilities": ["expand_intent", "detect_intent", "predict_tasks", "proactive_planning"],
    },
    {
        "agent_id": "execution_tier_router",
        "module": "backend.neural_mesh.agents.execution_tier_router",
        "class_name": "ExecutionTierRouter",
        "agent_type": "intelligence",
        "capabilities": ["decide_tier", "tier_routing"],
    },
    {
        "agent_id": "coordinator_agent",
        "module": "backend.neural_mesh.orchestration.coordinator_agent",
        "class_name": "CoordinatorAgent",
        "agent_type": "coordination",
        "capabilities": ["coordinate", "delegate_tasks", "balance_load"],
    },
    {
        "agent_id": "goal_inference_agent",
        "module": "backend.neural_mesh.agents.goal_inference_agent",
        "class_name": "GoalInferenceAgent",
        "agent_type": "intelligence",
        "capabilities": ["infer_goal", "classify_intent", "predict_next_goal"],
    },
    {
        "agent_id": "memory_agent",
        "module": "backend.neural_mesh.agents.memory_agent",
        "class_name": "MemoryAgent",
        "agent_type": "memory",
        "capabilities": ["store_memory", "retrieve_memory", "find_similar", "knowledge_retrieval"],
    },
    {
        "agent_id": "context_tracker_agent",
        "module": "backend.neural_mesh.agents.context_tracker_agent",
        "class_name": "ContextTrackerAgent",
        "agent_type": "context",
        "capabilities": ["track_context", "get_context", "get_history", "analyze_patterns"],
    },
    {
        "agent_id": "health_monitor_agent",
        "module": "backend.neural_mesh.agents.health_monitor_agent",
        "class_name": "HealthMonitorAgent",
        "agent_type": "monitoring",
        "capabilities": ["check_health", "get_metrics", "self_heal"],
    },
    {
        "agent_id": "error_analyzer_agent",
        "module": "backend.neural_mesh.agents.error_analyzer_agent",
        "class_name": "ErrorAnalyzerAgent",
        "agent_type": "analysis",
        "capabilities": ["analyze_error", "find_similar", "suggest_fix"],
    },
    {
        "agent_id": "pattern_recognition_agent",
        "module": "backend.neural_mesh.agents.pattern_recognition_agent",
        "class_name": "PatternRecognitionAgent",
        "agent_type": "analysis",
        "capabilities": ["detect_patterns", "find_anomalies", "predict_next", "correlate_events"],
    },
    {
        "agent_id": "visual_monitor_agent",
        "module": "backend.neural_mesh.agents.visual_monitor_agent",
        "class_name": "VisualMonitorAgent",
        "agent_type": "monitoring",
        "capabilities": ["watch_and_alert", "watch_multiple", "visual_monitor", "vision"],
    },
    {
        "agent_id": "app_inventory_service",
        "module": "backend.neural_mesh.agents.app_inventory_service",
        "class_name": "AppInventoryService",
        "agent_type": "intelligence",
        "capabilities": ["app_inventory", "check_app", "scan_installed"],
    },
]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _user_overrides_path() -> Path:
    env = os.getenv("JARVIS_AGENT_BINDINGS_PATH")
    if env:
        return Path(env)
    return Path.home() / ".jarvis" / "agent_bindings.json"


def load_bindings() -> Dict[str, AgentBinding]:
    """Load agent bindings: defaults merged with user overrides.

    User file is optional; if it exists, entries with matching ``agent_id``
    override defaults field-by-field.
    """
    # Start with defaults
    raw: Dict[str, Dict[str, Any]] = {
        entry["agent_id"]: dict(entry) for entry in _DEFAULT_BINDINGS
    }

    # Merge user overrides
    user_path = _user_overrides_path()
    if user_path.is_file():
        try:
            with open(user_path, "r") as f:
                user_entries = json.load(f)
            if isinstance(user_entries, list):
                for entry in user_entries:
                    aid = entry.get("agent_id")
                    if aid:
                        raw.setdefault(aid, {}).update(entry)
                logger.info("[AgentBindings] Merged %d user overrides from %s", len(user_entries), user_path)
        except Exception as exc:
            logger.warning("[AgentBindings] Failed to load user overrides from %s: %s", user_path, exc)

    # Convert to AgentBinding objects
    bindings: Dict[str, AgentBinding] = {}
    for aid, entry in raw.items():
        try:
            bindings[aid] = AgentBinding(
                agent_id=aid,
                module=entry["module"],
                class_name=entry["class_name"],
                agent_type=entry.get("agent_type", "general"),
                capabilities=set(entry.get("capabilities", [])),
                backend=entry.get("backend", "local"),
            )
        except KeyError as exc:
            logger.warning("[AgentBindings] Skipping %s: missing field %s", aid, exc)

    logger.info("[AgentBindings] Loaded %d agent bindings", len(bindings))
    return bindings


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_bindings: Optional[Dict[str, AgentBinding]] = None


def get_agent_bindings() -> Dict[str, AgentBinding]:
    """Get the singleton agent bindings (loaded on first call)."""
    global _bindings
    if _bindings is None:
        _bindings = load_bindings()
    return _bindings


def reload_bindings() -> Dict[str, AgentBinding]:
    """Force-reload bindings (e.g. after user edits the JSON)."""
    global _bindings
    _bindings = load_bindings()
    return _bindings

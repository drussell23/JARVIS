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
        "backend":       "local",
        "platforms":     ["darwin"],      # optional: sys.platform prefixes it runs on
        "requires":      ["playwright"],  # optional: importable packages it needs
        "route":         "",              # set for JARVIS's own routes (no class)
        "delivers":      "shown"          # what the USER gets: "shown" | "told" | "done" | ""
    }

``delivers`` is the one fact the Mind decides a request on -- does the user
want something SHOWN (put on their screen: a page, a site's results, an app)
or TOLD (an answer reported back)? Each entry states which it gives, in the
catalogue line, so the choice is made on that and not on a word the request
and a capability name happen to share: "search for dogs on firefox" was
reasoned "SHOWN ... in Firefox" and still routed to web_search_agent, whose
capabilities include "search" and which can only report an answer back.

A ``route`` entry is one of JARVIS's built-in ways of carrying out a request
(``open``: open something for the user; ``look``: answer by looking at the
screen). It sits in the same catalogue as the agents so the Mind chooses
between them on equal terms; without it the Mind mapped "open notepad" onto
the nearest agent capability name.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
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
    platforms: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    route: str = ""
    delivers: str = ""   # "shown" | "told" | "done" | "" (internal: none of them)
    # The action that takes a request in the user's own words (the agent
    # works out what to do itself), "" when the agent only takes the
    # capability it was chosen for as its action.
    entry: str = ""

    def platform_reason(self) -> str:
        """Why this binding can never serve on this host's platform, ""."""
        if self.platforms and not any(sys.platform.startswith(p) for p in self.platforms):
            return f"runs on {', '.join(self.platforms)}, not {sys.platform}"
        return ""

    def missing_requirements(self) -> List[str]:
        """Packages it needs that are not installed: it is not SET UP here,
        which an install fixes -- unlike a platform it cannot run on."""
        missing = []
        for requirement in self.requires:
            try:
                found = importlib.util.find_spec(requirement) is not None
            except (ImportError, ValueError):  # a dotted name whose parent is absent
                found = False
            if not found:
                missing.append(requirement)
        return missing

    def unavailable_reason(self) -> str:
        """Why this binding cannot serve on this host, "" when it can."""
        platform = self.platform_reason()
        if platform:
            return platform
        missing = self.missing_requirements()
        return f"needs {', '.join(missing)}" if missing else ""

    def describe(self) -> str:
        """First docstring line of what serves it (the class, or a route's module)."""
        target = importlib.import_module(self.module)
        if self.class_name:
            target = getattr(target, self.class_name)
        return next((ln.strip() for ln in (target.__doc__ or "").splitlines() if ln.strip()), "")

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
        "agent_id": "desktop_open",
        "module": "backend.core.desktop_open",
        "class_name": "",
        "agent_type": "route",
        "route": "open",
        "delivers": "shown",
        "capabilities": ["open_for_user"],
    },
    {
        "agent_id": "screen_question",
        "module": "backend.vision.screen_question",
        "class_name": "",
        "agent_type": "route",
        "route": "look",
        "delivers": "told",
        "capabilities": ["look_at_screen"],
    },
    {
        "agent_id": "desktop_close",
        "module": "backend.core.desktop_close",
        "class_name": "",
        "agent_type": "route",
        "route": "close",
        "delivers": "done",
        "capabilities": ["close_for_user"],
    },
    {
        "agent_id": "visual_browser_agent",
        "module": "backend.neural_mesh.agents.visual_browser_agent",
        "class_name": "VisualBrowserAgent",
        "agent_type": "vision",
        "delivers": "shown",
        "capabilities": ["visual_browser", "web_browsing", "browser", "browse_and_interact"],
        "requires": ["playwright"],
    },
    {
        "agent_id": "web_search_agent",
        "module": "backend.neural_mesh.agents.web_search_agent",
        "class_name": "WebSearchAgent",
        "agent_type": "intelligence",
        "delivers": "told",
        # What it delivers -- researched answers -- not the verb a request
        # uses: tagged "search"/"web_search", it drew every "search for X"
        # (the user asking for a search to be RUN) to a summary.
        "capabilities": ["web_research", "read_web_page"],
    },
    {
        "agent_id": "native_app_control_agent",
        "module": "backend.neural_mesh.agents.native_app_control_agent",
        "class_name": "NativeAppControlAgent",
        "agent_type": "control",
        "delivers": "shown",
        "capabilities": ["native_app_control", "interact_with_app", "app_control", "open_app"],
        "platforms": ["darwin"],   # drives apps through osascript
    },
    {
        "agent_id": "google_workspace_agent",
        "module": "backend.neural_mesh.agents.google_workspace_agent",
        "class_name": "GoogleWorkspaceAgent",
        "agent_type": "workspace",
        "delivers": "told",
        "capabilities": ["handle_workspace_query", "read_email", "check_calendar",
                         "send_email", "create_event", "email", "calendar"],
        "requires": ["googleapiclient", "google_auth_oauthlib"],
        "entry": "handle_workspace_query",
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
        "platforms": ["darwin"],   # Quartz / macOS video capture
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
                class_name=entry.get("class_name", ""),
                agent_type=entry.get("agent_type", "general"),
                capabilities=set(entry.get("capabilities", [])),
                backend=entry.get("backend", "local"),
                platforms=list(entry.get("platforms", [])),
                requires=list(entry.get("requires", [])),
                route=entry.get("route", ""),
                delivers=str(entry.get("delivers", "") or "").strip().lower(),
                entry=str(entry.get("entry", "") or "").strip(),
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


def binding_for_capability(capability: str) -> Optional[AgentBinding]:
    """The binding that declares *capability* and can serve on this host.

    Callers route by what a binding can do, never by its name, so a user
    override that rebinds a capability to another agent is honoured.
    """
    wanted = (capability or "").strip().lower()
    if not wanted:
        return None
    for binding in get_agent_bindings().values():
        # The catalogue shows each entry's id and its capabilities; the Mind
        # names either ("web_search_agent" as often as "web_research").
        names = {c.lower() for c in binding.capabilities} | {binding.agent_id.lower()}
        # A binding that is only not SET UP still serves the request: its
        # dispatch says exactly what it needs, where routing it elsewhere
        # had a model invent an answer ("I don't have access to your Gmail").
        if wanted in names and not binding.platform_reason():
            return binding
    return None


def agent_for_capability(capability: str) -> Optional[str]:
    """The agent_id of the available AGENT (not a route) declaring *capability*."""
    binding = binding_for_capability(capability)
    return binding.agent_id if binding is not None and not binding.route else None


def delivery_of(capability: str) -> str:
    """What the binding serving *capability* gives the user ("shown", "told",
    "done"), "" when unknown."""
    binding = binding_for_capability(capability)
    return binding.delivers if binding is not None else ""


def route_delivering(delivery: str) -> str:
    """A capability of JARVIS's own available route that gives *delivery*
    ("shown" -> the open route, "done" -> close), "" when there is none."""
    for binding in get_agent_bindings().values():
        if binding.route and binding.delivers == delivery and not binding.unavailable_reason():
            return next(iter(sorted(binding.capabilities)), binding.agent_id)
    return ""


def route_for_capability(capability: str) -> str:
    """The built-in route ("open", "look") serving *capability*, else ""."""
    binding = binding_for_capability(capability)
    return binding.route if binding is not None else ""


_catalogue: Optional[str] = None

# How a delivery reads in the catalogue: the words the classifier reasons in.
_DELIVERY_PHRASE = {
    "shown": "SHOWN: puts it on the user's screen",
    "told": "TOLD: reports an answer back, shows nothing",
    "done": "DONE: does it on the user's desktop (nothing new to show or tell)",
}


def capability_catalogue() -> str:
    """What each dispatchable agent does, for the Mind to choose from.

    One line per binding: ``agent_id — <its own docstring summary>
    [capabilities]``. Descriptions come from the agent classes themselves, so
    a user binding or a synthesized agent describes itself. A binding whose
    class cannot be imported cannot be dispatched and is not offered.
    Cached per bindings load (imports are not free); call from a thread.
    """
    global _catalogue
    if _catalogue is None:
        lines: List[str] = []
        for agent_id, binding in get_agent_bindings().items():
            why = binding.platform_reason()
            if why:
                logger.debug("[AgentBindings] %s not offered: %s", agent_id, why)
                continue
            missing = binding.missing_requirements()
            try:
                summary = binding.describe()
            except Exception as exc:  # noqa: BLE001 -- unimportable = undispatchable
                if not missing:
                    logger.debug("[AgentBindings] %s not offered: %s", agent_id, exc)
                    continue
                summary = agent_id.replace("_", " ")
            gives = _DELIVERY_PHRASE.get(binding.delivers, "")
            # Offered when only not set up: the request reaches it, and its
            # dispatch says what to install, instead of going elsewhere.
            setup = f" (not set up on this host: needs {', '.join(missing)})" if missing else ""
            lines.append(f"- {agent_id} — {summary}{setup} [{', '.join(sorted(binding.capabilities))}]"
                         + (f" -> {gives}" if gives else ""))
        _catalogue = "\n".join(lines)
    return _catalogue


def reload_bindings() -> Dict[str, AgentBinding]:
    """Force-reload bindings (e.g. after user edits the JSON)."""
    global _bindings, _catalogue
    _bindings = load_bindings()
    _catalogue = None
    return _bindings

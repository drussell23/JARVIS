"""
Symbiotic Router Facade — 3-tier dispatch with Strangler Fig migration.

The facade sits between the main JARVIS pipeline (RuntimeTaskOrchestrator,
WebSocket handler, etc.) and the organism's two nervous systems:

  Tier 1 — Core Contexts (Brain): 5 new execution environments
    The 397B Architect routes goals to Executor, Developer, Communicator,
    Observer, or Architect contexts. Feature-flagged per vertical.

  Tier 2 — Legacy Agents (Peripheral Nervous System): 22 Neural Mesh agents
    30K+ lines of production code (Google Workspace, Visual Monitor, etc.)
    Queried when Core Contexts can't handle the intent or are disabled.

  Tier 3 — Ouroboros Neuroplasticity (Pillar 6): CapabilityGapEvent
    When BOTH tiers fail, emit a CapabilityGapEvent to the GapSignalBus.
    The GraduationOrchestrator will JIT-synthesize the missing capability.

Strangler Fig Pattern: Core Contexts gradually absorb Legacy Agent logic.
Flip one vertical at a time (JARVIS_CTX_*=true), verify, flip the next.
Legacy agents stay alive as fallback until the migration is complete.

Usage from RuntimeTaskOrchestrator::

    from backend.core_contexts.facade import dispatch

    result = await dispatch(goal, task_type, command)
    if result is not None:
        return result  # Handled by Core Context OR Legacy Agent
    # result is None ONLY when Ouroboros was triggered (async synthesis)
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-vertical feature flags (all off by default — Strangler Fig)
# ---------------------------------------------------------------------------
_FLAGS = {
    "executor": os.environ.get("JARVIS_CTX_EXECUTOR", "false").lower() in ("true", "1", "yes"),
    "communicator": os.environ.get("JARVIS_CTX_COMMUNICATOR", "false").lower() in ("true", "1", "yes"),
    "developer": os.environ.get("JARVIS_CTX_DEVELOPER", "false").lower() in ("true", "1", "yes"),
    "observer": os.environ.get("JARVIS_CTX_OBSERVER", "false").lower() in ("true", "1", "yes"),
}

# Task type -> context mapping (deterministic — Tier 0 routing)
_TASK_TO_CONTEXT = {
    "vision_action": "executor",
    "vision_verification": "executor",
    "screen_observation": "executor",
    "browser_navigation": "executor",
    "email_compose": "communicator",
    "email_triage": "communicator",
    "email_summarization": "communicator",
    "calendar_query": "communicator",
    "complex_reasoning": "developer",
    "multi_step_planning": "developer",
    "proactive_narration": "observer",
}

# Legacy agent capability mapping — which agent handles which task types
# Used for Tier 2 fallback when Core Contexts are disabled or fail
_LEGACY_AGENT_MAP: Dict[str, str] = {
    "vision_action": "VisualBrowserAgent",
    "vision_verification": "VisualMonitorAgent",
    "screen_observation": "VisualMonitorAgent",
    "browser_navigation": "VisualBrowserAgent",
    "email_compose": "GoogleWorkspaceAgent",
    "email_triage": "GoogleWorkspaceAgent",
    "email_summarization": "GoogleWorkspaceAgent",
    "calendar_query": "GoogleWorkspaceAgent",
    "complex_reasoning": "PredictivePlanningAgent",
    "multi_step_planning": "CoordinatorAgent",
    "proactive_narration": "ActivityRecognitionAgent",
    "app_control": "NativeAppControlAgent",
    "spatial_query": "SpatialAwarenessAgent",
    "pattern_analysis": "PatternRecognitionAgent",
    "error_analysis": "ErrorAnalyzerAgent",
    "web_search": "WebSearchAgent",
    "memory_query": "MemoryAgent",
    "health_check": "HealthMonitorAgent",
    "context_track": "ContextTrackerAgent",
    "goal_inference": "GoalInferenceAgent",
}


async def dispatch(
    goal: str,
    task_type: str = "",
    command: str = "",
    step: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Symbiotic 3-tier dispatch: Core Contexts -> Legacy Agents -> Ouroboros.

    Args:
        goal: The user's goal in natural language.
        task_type: Semantic task type (e.g., "vision_action", "email_compose").
        command: Raw command text from user.
        step: Optional step dict from the DAG planner.

    Returns:
        Result dict if handled by any tier.
        None if Ouroboros synthesis was triggered (async, result comes later).
    """
    effective_command = command or goal

    # ── Tier 1: Core Contexts (Brain) ──────────────────────────────────
    context_name = _TASK_TO_CONTEXT.get(task_type)
    if context_name is None and effective_command:
        context_name = await _classify_to_context(effective_command)

    if context_name is not None and _FLAGS.get(context_name, False):
        logger.info(
            "[Facade] Tier 1: routing to %s context: %s",
            context_name.upper(), goal[:60],
        )
        try:
            result = await _execute_context(context_name, goal, task_type, command, step)
            if result is not None:
                return result
            # Context returned None — fall through to Tier 2
            logger.info(
                "[Facade] Tier 1 %s returned None, falling through to Tier 2",
                context_name,
            )
        except Exception as exc:
            logger.warning(
                "[Facade] Tier 1 %s failed: %s. Falling through to Tier 2.",
                context_name, exc,
            )

    # ── Tier 2: Legacy Agents (Peripheral Nervous System) ──────────────
    legacy_result = await _try_legacy_agent(goal, task_type, effective_command)
    if legacy_result is not None:
        return legacy_result

    # ── Tier 3: Ouroboros Neuroplasticity (Pillar 6) ───────────────────
    # Both Core Contexts AND Legacy Agents failed to handle this intent.
    # Emit a CapabilityGapEvent — the organism doesn't know how to do this YET.
    await _emit_capability_gap(goal, task_type, effective_command)

    # Return None — the caller should tell the user "I'm learning how to do this"
    return None


# ---------------------------------------------------------------------------
# Tier 1: Core Context execution (unchanged from original)
# ---------------------------------------------------------------------------

async def _execute_context(
    context_name: str,
    goal: str,
    task_type: str,
    command: str,
    step: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Execute a task in the specified Core Context."""

    if context_name == "executor":
        from backend.core_contexts.tools import apps

        for lean_path in ("backend.vision.lean_loop", "vision.lean_loop"):
            try:
                import importlib
                mod = importlib.import_module(lean_path)
                LeanVisionLoop = mod.LeanVisionLoop
                break
            except ImportError:
                continue
        else:
            return None  # Fall through to Tier 2

        loop = LeanVisionLoop.get_instance()
        target_app = (step or {}).get("target_app", "")
        if not target_app:
            goal_lower = goal.lower()
            for app in ("whatsapp", "safari", "chrome", "terminal", "mail",
                        "slack", "spotify", "notes", "finder", "messages"):
                if app in goal_lower:
                    target_app = app
                    break

        if target_app:
            await apps.open_app(target_app)
            await asyncio.sleep(2.0)

        return await loop.run(goal)

    elif context_name == "communicator":
        from backend.core_contexts.communicator import Communicator
        comm = Communicator()
        if "email" in goal.lower() and "check" in goal.lower():
            result = await comm.check_email()
            return {"success": result.success, "result": result.details, "data": result.data}
        elif "search" in goal.lower() or "find" in goal.lower():
            result = await comm.research(goal)
            return {"success": result.success, "result": result.details, "data": result.data}
        return None  # Unrecognized — fall through to Tier 2

    elif context_name == "developer":
        from backend.core_contexts.developer import Developer
        dev = Developer()
        if "error" in goal.lower() or "fix" in goal.lower() or "bug" in goal.lower():
            result = await dev.analyze_and_fix(goal)
            return {"success": result.success, "result": result.analysis, "suggestions": result.suggestions}
        return None  # Unrecognized — fall through to Tier 2

    elif context_name == "observer":
        from backend.core_contexts.observer import Observer
        obs = Observer()
        observation = await obs.health_check()
        return {
            "success": True,
            "result": observation.summary,
            "severity": observation.severity,
            "details": observation.details,
        }

    return None


# ---------------------------------------------------------------------------
# Tier 2: Legacy Agent fallback (Peripheral Nervous System)
# ---------------------------------------------------------------------------

async def _try_legacy_agent(
    goal: str,
    task_type: str,
    command: str,
) -> Optional[Dict[str, Any]]:
    """Try to handle the intent via a Legacy Neural Mesh agent.

    Queries the agent registry for a matching agent, calls execute_task(),
    and returns the result. Returns None if no agent can handle it.
    """
    # Determine which legacy agent handles this task type
    agent_class_name = _LEGACY_AGENT_MAP.get(task_type)

    # If no task_type mapping, try keyword-based agent selection
    if agent_class_name is None:
        agent_class_name = _classify_to_legacy_agent(command)

    if agent_class_name is None:
        logger.debug("[Facade] Tier 2: no legacy agent mapped for task_type=%s", task_type)
        return None

    try:
        # Lazy-import the specific agent (avoid loading all 22 agents)
        agent_instance = _get_legacy_agent(agent_class_name)
        if agent_instance is None:
            logger.debug("[Facade] Tier 2: %s not available", agent_class_name)
            return None

        logger.info(
            "[Facade] Tier 2: routing to legacy %s: %s",
            agent_class_name, goal[:60],
        )

        result = await asyncio.wait_for(
            agent_instance.execute_task({
                "action": task_type or "execute",
                "goal": goal,
                "command": command,
            }),
            timeout=30.0,
        )

        if result and isinstance(result, dict):
            # Normalize legacy agent results
            if "status" in result or "result" in result:
                return {
                    "success": result.get("status") == "ok" or result.get("success", False),
                    "result": result.get("result", result.get("status", "")),
                    "source": f"legacy:{agent_class_name}",
                    **{k: v for k, v in result.items() if k not in ("status", "result", "success")},
                }
            return result

    except asyncio.TimeoutError:
        logger.warning("[Facade] Tier 2: %s timed out after 30s", agent_class_name)
    except Exception as exc:
        logger.warning("[Facade] Tier 2: %s failed: %s", agent_class_name, exc)

    return None


def _get_legacy_agent(class_name: str) -> Optional[Any]:
    """Lazy-load a legacy agent instance by class name.

    Uses the agent_initializer's singleton pattern if available,
    falls back to direct instantiation.
    """
    try:
        # Try to get from the running agent registry
        from backend.neural_mesh.agents.agent_initializer import get_agent_initializer
        initializer = get_agent_initializer()
        if initializer is not None:
            agent = initializer.get_agent(class_name)
            if agent is not None:
                return agent
    except Exception:
        pass

    # Direct instantiation fallback
    try:
        import importlib
        module_name = _class_to_module(class_name)
        mod = importlib.import_module(f"backend.neural_mesh.agents.{module_name}")
        cls = getattr(mod, class_name)
        return cls()
    except Exception:
        return None


def _class_to_module(class_name: str) -> str:
    """Convert CamelCase agent class name to snake_case module name.

    GoogleWorkspaceAgent -> google_workspace_agent
    VisualMonitorAgent -> visual_monitor_agent
    """
    import re
    s = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", class_name)
    return s.lower()


def _classify_to_legacy_agent(command: str) -> Optional[str]:
    """Map a command to a legacy agent class name via keywords.

    Deterministic keyword matching — no model inference.
    This is Tier 0 routing for the legacy path.
    """
    cmd = command.lower()

    if any(w in cmd for w in ("email", "gmail", "calendar", "drive", "workspace")):
        return "GoogleWorkspaceAgent"
    if any(w in cmd for w in ("screenshot", "screen", "visual", "monitor")):
        return "VisualMonitorAgent"
    if any(w in cmd for w in ("browse", "navigate", "website", "url")):
        return "VisualBrowserAgent"
    if any(w in cmd for w in ("app", "launch", "quit", "close", "window")):
        return "NativeAppControlAgent"
    if any(w in cmd for w in ("search", "web", "lookup", "google")):
        return "WebSearchAgent"
    if any(w in cmd for w in ("error", "crash", "traceback", "exception")):
        return "ErrorAnalyzerAgent"
    if any(w in cmd for w in ("remember", "recall", "memory", "forgot")):
        return "MemoryAgent"
    if any(w in cmd for w in ("plan", "predict", "anticipate", "next")):
        return "PredictivePlanningAgent"
    if any(w in cmd for w in ("coordinate", "delegate", "assign")):
        return "CoordinatorAgent"
    if any(w in cmd for w in ("pattern", "trend", "recurring")):
        return "PatternRecognitionAgent"
    if any(w in cmd for w in ("health", "status", "diagnostics")):
        return "HealthMonitorAgent"

    return None


# ---------------------------------------------------------------------------
# Tier 3: Ouroboros Neuroplasticity (CapabilityGapEvent emission)
# ---------------------------------------------------------------------------

async def _emit_capability_gap(goal: str, task_type: str, command: str) -> None:
    """Emit a CapabilityGapEvent when both Core Contexts and Legacy Agents fail.

    The GapSignalBus routes this to the CapabilityGapSensor, which feeds it
    into the Ouroboros pipeline. The GraduationOrchestrator may synthesize
    a new agent to handle this class of request in the future.
    """
    try:
        from backend.neural_mesh.synthesis.gap_signal_bus import (
            GapSignalBus,
            CapabilityGapEvent,
            get_gap_signal_bus,
        )
        bus = get_gap_signal_bus()
        event = CapabilityGapEvent(
            goal=goal,
            task_type=task_type or "unknown",
            target_app="",
            source="facade_router",
            resolution_mode="synthesis",
        )
        bus.emit(event)
        logger.warning(
            "[Facade] Tier 3: CapabilityGapEvent emitted — neither Core Contexts "
            "nor Legacy Agents could handle: %s (task_type=%s). "
            "Ouroboros neuroplasticity triggered.",
            goal[:80], task_type,
        )
    except Exception as exc:
        logger.error(
            "[Facade] Tier 3: Failed to emit CapabilityGapEvent: %s. "
            "Goal '%s' was unhandled with no synthesis fallback.",
            exc, goal[:60],
        )


# ---------------------------------------------------------------------------
# Tier 1 intent classification (unchanged)
# ---------------------------------------------------------------------------

async def _classify_to_context(command: str) -> Optional[str]:
    """Use intent classification to determine the Core Context.

    Falls back to keyword matching if intelligence tools are unavailable.
    """
    try:
        from backend.core_contexts.tools.intelligence import classify_intent
        result = await asyncio.wait_for(classify_intent(command), timeout=3.0)
        return result.suggested_context or None
    except Exception:
        cmd = command.lower()
        if any(w in cmd for w in ("open", "click", "type", "whatsapp", "safari", "chrome")):
            return "executor"
        if any(w in cmd for w in ("email", "mail", "calendar", "schedule", "message")):
            return "communicator"
        if any(w in cmd for w in ("fix", "bug", "error", "code", "refactor")):
            return "developer"
        if any(w in cmd for w in ("monitor", "health", "watch", "alert")):
            return "observer"
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_enabled_contexts() -> Dict[str, bool]:
    """Return the current state of all context flags."""
    return dict(_FLAGS)


def is_context_enabled(context_name: str) -> bool:
    """Check if a specific context is enabled."""
    return _FLAGS.get(context_name, False)

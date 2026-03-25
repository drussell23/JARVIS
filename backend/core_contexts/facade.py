"""
Core Contexts Dispatch Facade -- single entry point for the main pipeline.

The facade sits between the main JARVIS pipeline (RuntimeTaskOrchestrator,
WebSocket handler, etc.) and the Core Contexts.  It decides whether to
route through the NEW Core Context path or the LEGACY agent path based
on per-vertical feature flags.

This enables incremental migration: flip one vertical at a time, verify
it works, then flip the next.  No big bang.

Feature flags (all default False until explicitly enabled):
    JARVIS_CTX_EXECUTOR=true       -- vision/UI tasks via Executor context
    JARVIS_CTX_COMMUNICATOR=true   -- email/calendar via Communicator context
    JARVIS_CTX_DEVELOPER=true      -- code/error tasks via Developer context
    JARVIS_CTX_OBSERVER=true       -- monitoring via Observer context

When a flag is False, the facade returns None and the caller falls
through to the legacy agent path.  Zero disruption.

Usage from RuntimeTaskOrchestrator::

    from backend.core_contexts.facade import dispatch

    result = await dispatch(goal, task_type, command)
    if result is not None:
        return result  # Core Context handled it
    # else: fall through to legacy agent path
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Per-vertical feature flags (all off by default)
_FLAGS = {
    "executor": os.environ.get("JARVIS_CTX_EXECUTOR", "false").lower() in ("true", "1", "yes"),
    "communicator": os.environ.get("JARVIS_CTX_COMMUNICATOR", "false").lower() in ("true", "1", "yes"),
    "developer": os.environ.get("JARVIS_CTX_DEVELOPER", "false").lower() in ("true", "1", "yes"),
    "observer": os.environ.get("JARVIS_CTX_OBSERVER", "false").lower() in ("true", "1", "yes"),
}

# Task type -> context mapping
_TASK_TO_CONTEXT = {
    # Executor
    "vision_action": "executor",
    "vision_verification": "executor",
    "screen_observation": "executor",
    "browser_navigation": "executor",
    # Communicator
    "email_compose": "communicator",
    "email_triage": "communicator",
    "email_summarization": "communicator",
    "calendar_query": "communicator",
    # Developer
    "complex_reasoning": "developer",
    "multi_step_planning": "developer",
    # Observer
    "proactive_narration": "observer",
}


async def dispatch(
    goal: str,
    task_type: str = "",
    command: str = "",
    step: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch a task through the Core Context system if the vertical is enabled.

    This is the single entry point that the main pipeline calls.  If the
    relevant context's feature flag is enabled, the task is handled by the
    new Core Context path.  If not, returns None so the caller falls through
    to the legacy agent path.

    Args:
        goal: The user's goal in natural language.
        task_type: Semantic task type (e.g., "vision_action", "email_compose").
        command: Raw command text from user.
        step: Optional step dict from the DAG planner.

    Returns:
        Result dict if handled by Core Context.
        None if the vertical is not enabled (fall through to legacy).
    """
    # Determine which context handles this task type
    context_name = _TASK_TO_CONTEXT.get(task_type)

    # If no mapping, try intent classification
    if context_name is None and command:
        context_name = await _classify_to_context(command)

    if context_name is None:
        return None  # No context identified -- fall through

    # Check if this vertical's flag is enabled
    if not _FLAGS.get(context_name, False):
        logger.debug(
            "[Facade] %s context not enabled (JARVIS_CTX_%s=false), falling through to legacy",
            context_name, context_name.upper(),
        )
        return None

    logger.info(
        "[Facade] Routing to %s context: %s",
        context_name.upper(), goal[:60],
    )

    try:
        return await _execute_context(context_name, goal, task_type, command, step)
    except Exception as exc:
        logger.error(
            "[Facade] %s context failed: %s. Falling through to legacy.",
            context_name, exc,
        )
        return None  # Fall through on error


async def _execute_context(
    context_name: str,
    goal: str,
    task_type: str,
    command: str,
    step: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Execute a task in the specified Core Context."""

    if context_name == "executor":
        from backend.core_contexts.executor import Executor
        from backend.core_contexts.tools import screen, input as input_tools, apps

        # For vision tasks, use the lean loop (already battle-tested)
        if task_type in ("vision_action", "browser_navigation"):
            from backend.vision.lean_loop import LeanVisionLoop
            loop = LeanVisionLoop.get_instance()

            # Open app first if specified
            target_app = (step or {}).get("target_app", "")
            if target_app:
                await apps.open_app(target_app)
                await asyncio.sleep(2.0)

            result = await loop.run(goal)
            return result

        # For screen observation, capture and describe
        frame = await screen.capture_and_compress()
        if frame:
            return {"success": True, "result": f"Screen captured ({frame.width}x{frame.height})"}
        return {"success": False, "result": "Screen capture failed"}

    elif context_name == "communicator":
        from backend.core_contexts.communicator import Communicator
        comm = Communicator()

        if "email" in goal.lower() and "check" in goal.lower():
            result = await comm.check_email()
            return {"success": result.success, "result": result.details, "data": result.data}
        elif "search" in goal.lower() or "find" in goal.lower():
            result = await comm.research(goal)
            return {"success": result.success, "result": result.details, "data": result.data}
        else:
            return {"success": False, "result": "Communicator: unrecognized goal pattern"}

    elif context_name == "developer":
        from backend.core_contexts.developer import Developer
        dev = Developer()

        if "error" in goal.lower() or "fix" in goal.lower() or "bug" in goal.lower():
            result = await dev.analyze_and_fix(goal)
            return {"success": result.success, "result": result.analysis, "suggestions": result.suggestions}
        return {"success": False, "result": "Developer: unrecognized goal pattern"}

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


async def _classify_to_context(command: str) -> Optional[str]:
    """Use intent classification to determine the context.

    Falls back to keyword matching if the intelligence tools are unavailable.
    """
    try:
        from backend.core_contexts.tools.intelligence import classify_intent
        result = await asyncio.wait_for(classify_intent(command), timeout=3.0)
        return result.suggested_context or None
    except Exception:
        # Keyword fallback
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


def get_enabled_contexts() -> Dict[str, bool]:
    """Return the current state of all context flags."""
    return dict(_FLAGS)


def is_context_enabled(context_name: str) -> bool:
    """Check if a specific context is enabled."""
    return _FLAGS.get(context_name, False)

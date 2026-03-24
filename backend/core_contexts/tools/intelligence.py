"""
Atomic intelligence tools -- intent classification, anomaly detection, pattern recognition.

These tools provide the Architect and Observer contexts with analytical
capabilities for understanding user intent, detecting unusual behavior,
and recognizing recurring patterns in system activity.

Delegates to GoalInferenceAgent, PatternRecognitionAgent, and
ContextTrackerAgent.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_INFERENCE_TIMEOUT_S = float(os.environ.get("TOOL_INFERENCE_TIMEOUT_S", "5.0"))


@dataclass(frozen=True)
class IntentClassification:
    """Result of classifying a user command's intent.

    Attributes:
        category: Intent category (e.g., "communication", "coding",
            "system_control", "research", "creative").
        level: Complexity level ("trivial", "light", "heavy", "complex").
        confidence: Classification confidence (0.0 to 1.0).
        matched_keywords: Keywords that triggered this classification.
        suggested_context: Which Core Context should handle this
            ("executor", "architect", "developer", "communicator", "observer").
    """
    category: str
    level: str
    confidence: float
    matched_keywords: List[str] = field(default_factory=list)
    suggested_context: str = ""


@dataclass(frozen=True)
class DetectedAnomaly:
    """An anomaly detected in system metrics or behavior.

    Attributes:
        metric_name: Name of the metric that is anomalous.
        current_value: The current (anomalous) value.
        expected_value: The expected (normal) value.
        z_score: How many standard deviations from normal.
        severity: "low", "medium", "high".
        description: Human-readable explanation.
    """
    metric_name: str
    current_value: float
    expected_value: float
    z_score: float
    severity: str
    description: str


@dataclass(frozen=True)
class RecurringPattern:
    """A detected recurring pattern in user or system behavior.

    Attributes:
        pattern_type: Type of pattern ("temporal", "behavioral", "correlation").
        description: Human-readable description of the pattern.
        confidence: Detection confidence (0.0 to 1.0).
        occurrences: How many times this pattern has been observed.
        peak_hour: For temporal patterns, the hour with highest activity.
    """
    pattern_type: str
    description: str
    confidence: float
    occurrences: int = 0
    peak_hour: int = -1


async def classify_intent(command: str) -> IntentClassification:
    """Classify a user command's intent category and complexity level.

    Analyzes the command text to determine what the user wants to do
    and how complex the task is.  This drives the Architect's decision
    about which Core Context and model to use.

    Args:
        command: Raw command text from the user (voice or typed).

    Returns:
        IntentClassification with category, level, confidence, and
        suggested context.

    Use when:
        The Architect receives a new command and needs to decide how to
        route it (which context, which model, how many steps).
    """
    agent = await _get_goal_agent()
    if agent is not None:
        try:
            result = await asyncio.wait_for(
                agent._classify_intent({
                    "action": "classify_intent",
                    "command": command,
                }),
                timeout=_INFERENCE_TIMEOUT_S,
            )
            category = result.get("category", "general")
            level = result.get("level", "light")
            return IntentClassification(
                category=category,
                level=level,
                confidence=result.get("confidence", 0.7),
                matched_keywords=result.get("matched_keywords", []),
                suggested_context=_category_to_context(category),
            )
        except Exception as exc:
            logger.debug("[tool:intelligence] classify_intent agent failed: %s", exc)

    return _fallback_classify(command)


async def detect_anomalies(
    metrics: Dict[str, float],
    sensitivity: float = 2.0,
) -> List[DetectedAnomaly]:
    """Detect anomalous values in a set of system metrics.

    Compares each metric against its historical baseline using z-score
    analysis.  Metrics with z-scores exceeding the sensitivity threshold
    are flagged as anomalous.

    Args:
        metrics: Dict of metric_name -> current_value
            (e.g., {"cpu_percent": 95.0, "memory_percent": 88.0}).
        sensitivity: Z-score threshold (default 2.0 = ~95th percentile).
            Lower values catch more anomalies but increase false positives.

    Returns:
        List of DetectedAnomaly for metrics that exceed the threshold.
        Empty list if all metrics are within normal range.

    Use when:
        The Observer needs to check if the system is behaving normally
        or if something unusual is happening (high CPU, memory leak, etc.).
    """
    agent = await _get_pattern_agent()
    if agent is not None:
        try:
            result = await asyncio.wait_for(
                agent._find_anomalies({
                    "action": "find_anomalies",
                    "data": [{"value": v, "metric": k} for k, v in metrics.items()],
                    "sensitivity": sensitivity,
                }),
                timeout=_INFERENCE_TIMEOUT_S,
            )
            return [
                DetectedAnomaly(
                    metric_name=a.get("metric", ""),
                    current_value=a.get("value", 0.0),
                    expected_value=a.get("mean", 0.0),
                    z_score=a.get("z_score", 0.0),
                    severity="high" if a.get("z_score", 0) > 3 else "medium",
                    description=a.get("description", ""),
                )
                for a in result.get("anomalies", [])
            ]
        except Exception as exc:
            logger.debug("[tool:intelligence] detect_anomalies failed: %s", exc)

    return []


async def detect_patterns(
    events: List[Dict[str, Any]],
) -> List[RecurringPattern]:
    """Detect recurring temporal and behavioral patterns in event data.

    Analyzes a list of timestamped events to find patterns like:
    - Temporal: "User unlocks at 7 AM every weekday"
    - Behavioral: "After opening Terminal, user always opens Chrome"
    - Correlation: "High CPU correlates with browser tab count"

    Args:
        events: List of event dicts, each with at least a "timestamp"
            field (ISO format or POSIX) and a "type" or "action" field.

    Returns:
        List of RecurringPattern with pattern descriptions and confidence.

    Use when:
        The Observer has accumulated enough event data to look for
        patterns that could inform proactive behavior (e.g., pre-opening
        apps the user typically needs at this time of day).
    """
    agent = await _get_pattern_agent()
    if agent is not None:
        try:
            result = await asyncio.wait_for(
                agent._detect_patterns({"action": "detect_patterns", "data": events}),
                timeout=_INFERENCE_TIMEOUT_S,
            )
            return [
                RecurringPattern(
                    pattern_type=p.get("type", "temporal"),
                    description=p.get("description", ""),
                    confidence=p.get("confidence", 0.5),
                    occurrences=p.get("occurrences", 0),
                    peak_hour=p.get("peak_hour", -1),
                )
                for p in result.get("patterns", [])
            ]
        except Exception as exc:
            logger.debug("[tool:intelligence] detect_patterns failed: %s", exc)

    return []


async def get_environment_context() -> Dict[str, Any]:
    """Get the current system and user environment context.

    Returns information about the current time, day of week, active
    session duration, platform, and user environment that can inform
    context-aware decisions.

    Returns:
        Dict with: time_context (hour, period, day_of_week),
        system (platform, python_version, hostname),
        environment (cwd, user, home_dir).

    Use when:
        The Architect needs temporal or environmental context to make
        better routing decisions (e.g., "it's 3 AM, the user might want
        quieter responses" or "it's Monday morning, check calendar").
    """
    import platform
    from datetime import datetime

    now = datetime.now()
    hour = now.hour
    if hour < 6:
        period = "late_night"
    elif hour < 12:
        period = "morning"
    elif hour < 17:
        period = "afternoon"
    elif hour < 21:
        period = "evening"
    else:
        period = "night"

    return {
        "time_context": {
            "hour": hour,
            "period": period,
            "day_of_week": now.strftime("%A"),
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.isoformat(),
        },
        "system": {
            "platform": platform.system(),
            "python_version": platform.python_version(),
            "hostname": platform.node(),
        },
        "environment": {
            "cwd": os.getcwd(),
            "user": os.environ.get("USER", ""),
            "home_dir": os.path.expanduser("~"),
        },
    }


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

_goal_agent = None
_pattern_agent = None


async def _get_goal_agent():
    global _goal_agent
    if _goal_agent is not None:
        return _goal_agent
    for path in ("backend.neural_mesh.agents.goal_inference_agent",
                 "neural_mesh.agents.goal_inference_agent"):
        try:
            import importlib
            mod = importlib.import_module(path)
            _goal_agent = mod.GoalInferenceAgent()
            return _goal_agent
        except (ImportError, Exception):
            continue
    return None


async def _get_pattern_agent():
    global _pattern_agent
    if _pattern_agent is not None:
        return _pattern_agent
    for path in ("backend.neural_mesh.agents.pattern_recognition_agent",
                 "neural_mesh.agents.pattern_recognition_agent"):
        try:
            import importlib
            mod = importlib.import_module(path)
            _pattern_agent = mod.PatternRecognitionAgent()
            return _pattern_agent
        except (ImportError, Exception):
            continue
    return None


def _category_to_context(category: str) -> str:
    """Map intent category to Core Context."""
    mapping = {
        "communication": "communicator",
        "email": "communicator",
        "messaging": "communicator",
        "calendar": "communicator",
        "coding": "developer",
        "debugging": "developer",
        "development": "developer",
        "system_control": "executor",
        "app_control": "executor",
        "browser": "executor",
        "monitoring": "observer",
        "analysis": "observer",
        "research": "architect",
        "planning": "architect",
    }
    return mapping.get(category, "architect")


def _fallback_classify(command: str) -> IntentClassification:
    """Basic intent classification without the full agent."""
    cmd = command.lower()
    if any(w in cmd for w in ("open", "click", "type", "scroll", "navigate")):
        return IntentClassification("app_control", "heavy", 0.6, suggested_context="executor")
    if any(w in cmd for w in ("email", "mail", "send", "message", "calendar")):
        return IntentClassification("communication", "heavy", 0.6, suggested_context="communicator")
    if any(w in cmd for w in ("fix", "bug", "error", "debug", "refactor", "code")):
        return IntentClassification("coding", "complex", 0.6, suggested_context="developer")
    if any(w in cmd for w in ("search", "find", "look up", "research")):
        return IntentClassification("research", "light", 0.6, suggested_context="architect")
    return IntentClassification("general", "light", 0.5, suggested_context="architect")

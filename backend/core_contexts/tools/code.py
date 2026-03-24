"""
Atomic code analysis and generation tools.

These tools provide the Developer context with error analysis,
fix suggestions, and integration with the Ouroboros governance
pipeline for code generation and testing.

Delegates to ErrorAnalyzerAgent and the Ouroboros infrastructure.

The 397B Architect selects these tools by reading docstrings.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ANALYSIS_TIMEOUT_S = float(os.environ.get("TOOL_CODE_ANALYSIS_TIMEOUT_S", "15.0"))


@dataclass(frozen=True)
class ErrorAnalysis:
    """Result of analyzing an error.

    Attributes:
        error_type: Classified error category (e.g., "ImportError", "timeout", "permission").
        severity: "low", "medium", "high", or "critical".
        root_cause: Best-guess explanation of why the error occurred.
        suggestions: List of actionable fix suggestions.
        similar_count: Number of similar past errors found in memory.
        pattern_key: Internal pattern key for tracking recurrence.
    """
    error_type: str
    severity: str
    root_cause: str
    suggestions: List[str]
    similar_count: int = 0
    pattern_key: str = ""


@dataclass(frozen=True)
class SimilarError:
    """A previously encountered similar error.

    Attributes:
        error_text: The original error message.
        solution: How it was resolved (empty if unresolved).
        timestamp: When it was first seen.
        occurrences: How many times this pattern has occurred.
    """
    error_text: str
    solution: str
    timestamp: str
    occurrences: int


async def analyze_error(
    error_message: str,
    traceback: str = "",
    context: str = "",
) -> ErrorAnalysis:
    """Analyze an error message and suggest fixes.

    Classifies the error type, estimates severity, identifies the likely
    root cause, and generates actionable fix suggestions.  Also searches
    memory for similar past errors and their solutions.

    Args:
        error_message: The error message text (e.g., "ModuleNotFoundError: No module named 'foo'").
        traceback: Optional full traceback string for deeper analysis.
        context: Optional description of what was happening when the error occurred.

    Returns:
        ErrorAnalysis with classification, root cause, and suggestions.

    Use when:
        The Developer encounters an error during code execution, testing,
        or deployment and needs to understand what went wrong and how to fix it.
    """
    agent = await _get_error_agent()
    if agent is None:
        return _fallback_analysis(error_message)

    try:
        result = await asyncio.wait_for(
            agent._analyze_error({
                "action": "analyze_error",
                "error": error_message,
                "traceback": traceback,
                "context": context,
            }),
            timeout=_ANALYSIS_TIMEOUT_S,
        )
        return ErrorAnalysis(
            error_type=result.get("error_type", "unknown"),
            severity=result.get("severity", "medium"),
            root_cause=result.get("root_cause", ""),
            suggestions=result.get("suggestions", []),
            similar_count=result.get("similar_count", 0),
            pattern_key=result.get("pattern_key", ""),
        )
    except Exception as exc:
        logger.error("[tool:code] analyze_error failed: %s", exc)
        return _fallback_analysis(error_message)


async def find_similar_errors(
    error_message: str,
    limit: int = 5,
) -> List[SimilarError]:
    """Search memory for similar past errors and their solutions.

    Uses semantic similarity search against the knowledge graph to find
    errors that match the current one.  Past solutions are returned if
    available.

    Args:
        error_message: The error to search for similar matches.
        limit: Maximum number of similar errors to return.

    Returns:
        List of SimilarError objects with past errors and solutions.

    Use when:
        The Developer wants to check if this error has been seen before
        and how it was resolved, before attempting a new fix.
    """
    agent = await _get_error_agent()
    if agent is None:
        return []

    try:
        result = await asyncio.wait_for(
            agent._find_similar_errors({
                "action": "find_similar",
                "error": error_message,
                "limit": limit,
            }),
            timeout=_ANALYSIS_TIMEOUT_S,
        )
        return [
            SimilarError(
                error_text=e.get("error", ""),
                solution=e.get("solution", ""),
                timestamp=e.get("timestamp", ""),
                occurrences=e.get("occurrences", 1),
            )
            for e in result.get("similar_errors", [])
        ]
    except Exception as exc:
        logger.error("[tool:code] find_similar_errors failed: %s", exc)
        return []


async def suggest_fix(error_message: str, context: str = "") -> List[str]:
    """Generate fix suggestions for an error.

    Combines error pattern analysis with knowledge graph lookup to
    produce actionable fix suggestions.

    Args:
        error_message: The error to fix.
        context: Optional context about what was being attempted.

    Returns:
        List of actionable fix suggestions (strings).

    Use when:
        The Developer needs quick fix ideas without full error analysis.
    """
    analysis = await analyze_error(error_message, context=context)
    return analysis.suggestions


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

_error_agent = None


async def _get_error_agent():
    global _error_agent
    if _error_agent is not None:
        return _error_agent

    for import_path in ("backend.neural_mesh.agents.error_analyzer_agent",
                        "neural_mesh.agents.error_analyzer_agent"):
        try:
            import importlib
            mod = importlib.import_module(import_path)
            cls = mod.ErrorAnalyzerAgent
            _error_agent = cls()
            return _error_agent
        except (ImportError, Exception):
            continue
    return None


def _fallback_analysis(error_message: str) -> ErrorAnalysis:
    """Basic error classification without the full agent."""
    msg_lower = error_message.lower()
    if "import" in msg_lower:
        return ErrorAnalysis("ImportError", "medium", "Missing module or package",
                           ["pip install the missing package", "Check PYTHONPATH"])
    if "timeout" in msg_lower:
        return ErrorAnalysis("TimeoutError", "medium", "Operation exceeded time limit",
                           ["Increase timeout", "Check network connectivity"])
    if "permission" in msg_lower:
        return ErrorAnalysis("PermissionError", "high", "Insufficient permissions",
                           ["Check file permissions", "Run with appropriate privileges"])
    return ErrorAnalysis("Unknown", "medium", error_message[:200], ["Check logs for details"])

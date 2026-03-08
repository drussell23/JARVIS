"""backend/core/ouroboros/governance/comms/narrator_script.py

Message templates for voice narration at pipeline milestones.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

from typing import Any, Dict

SCRIPTS: Dict[str, str] = {
    "signal_detected": (
        "Derek, I noticed {test_count} test failure{s} in {file}. "
        "Analyzing the issue now."
    ),
    "generating": "I'm generating a fix for {file} via {provider}.",
    "approve": (
        "I'd like to modify {file} to {goal}. "
        "This is approval-required. "
        "Use the CLI to approve or reject op {op_id}."
    ),
    "applied": "Fix applied and verified. {file} -- all tests passing now.",
    "postmortem": (
        "The fix for {file} didn't work. I've rolled back the changes. "
        "Reason: {reason}."
    ),
    "observe_error": (
        "I'm seeing repeated errors in {file} -- {error_summary}. "
        "Want me to investigate?"
    ),
    "cross_repo_impact": (
        "Heads up -- this change to {file} in {repo} affects "
        "{affected_count} file{s} in {other_repos}."
    ),
}

_FALLBACK = "Pipeline update for op {op_id}: phase {phase}."


def format_narration(phase: str, context: Dict[str, Any]) -> str:
    """Format a narration message for the given phase.

    Uses safe formatting -- missing keys are replaced with '?'
    rather than raising KeyError.
    """
    template = SCRIPTS.get(phase, _FALLBACK)
    # Add defaults for common placeholders
    safe_ctx: Dict[str, Any] = {
        "s": "s",
        "phase": phase,
        "op_id": "unknown",
        "file": "unknown",
        "provider": "unknown",
        "goal": "unknown",
        "reason": "unknown",
        "error_summary": "unknown",
        "test_count": "?",
        "repo": "unknown",
        "affected_count": "?",
        "other_repos": "unknown",
    }
    safe_ctx.update(context)
    # Pluralization
    if safe_ctx.get("test_count") == 1:
        safe_ctx["s"] = ""
    try:
        return template.format(**safe_ctx)
    except (KeyError, IndexError, ValueError):
        return f"Pipeline update: {phase} -- {context}"

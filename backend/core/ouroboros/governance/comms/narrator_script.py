"""backend/core/ouroboros/governance/comms/narrator_script.py

Message templates for voice narration at pipeline milestones.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4

Templates use only keys that the CommProtocol emit_* methods actually provide,
so narration is always accurate — no placeholder text like "?" or "unknown".
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ── Templates ────────────────────────────────────────────────────────────────
# Keys available per emit method (from comm_protocol.py):
#   emit_intent:    goal, target_files, risk_tier, blast_radius
#   emit_decision:  outcome, reason_code, diff_summary?
#   emit_postmortem: root_cause, failed_phase, next_safe_action?
#   VoiceNarrator also injects: file (from target_files[0]), op_id

SCRIPTS: Dict[str, str] = {
    # INTENT with test_count (from TestFailureSensor)
    "signal_detected_with_count": (
        "Derek, I noticed {test_count} test failure{s} in {file}. "
        "Analyzing the issue now."
    ),
    # INTENT generic (from emit_intent — has goal + file)
    "signal_detected": (
        "Derek, I'm analyzing {file}. Goal: {goal}."
    ),
    "generating": "I'm generating a fix for {file} via {provider}.",
    "approve": (
        "I'd like to modify {file} to {goal}. "
        "This is approval-required. "
        "Use the CLI to approve or reject op {op_id}."
    ),
    # DECISION — uses outcome (always present from emit_decision)
    "applied_with_file": "Fix applied and verified for {file}.",
    "applied": "Operation complete. Outcome: {outcome}.",
    # POSTMORTEM — uses root_cause (always present from emit_postmortem)
    "postmortem_with_file": (
        "The fix for {file} didn't work. I've rolled back the changes. "
        "Root cause: {root_cause}."
    ),
    "postmortem": (
        "Operation rolled back. Root cause: {root_cause}."
    ),
    "observe_error": (
        "I'm seeing repeated errors in {file} -- {error_summary}. "
        "Want me to investigate?"
    ),
    "cross_repo_impact": (
        "Heads up -- this change to {file} in {repo} affects "
        "{affected_count} file{s} in {other_repos}."
    ),
    "duplication_blocked": (
        "I blocked a code change for {file}. "
        "The generated code duplicated existing logic."
    ),
    "similarity_escalated": (
        "A code change for {file} has high overlap with existing code. "
        "Escalating for your review."
    ),
    "verify_regression": (
        "I rolled back a change to {file}. "
        "Post-apply verification failed: {root_cause}."
    ),
}

_FALLBACK = "Pipeline update for op {op_id}: phase {phase}."

# ── Required keys per template ───────────────────────────────────────────────
# Narration is suppressed if ANY required key is missing or sentinel.

_REQUIRED_KEYS: Dict[str, tuple] = {
    "signal_detected":            ("file", "goal"),
    "signal_detected_with_count": ("file", "test_count"),
    "generating":                 ("file", "provider"),
    "approve":                    ("file", "goal", "op_id"),
    "applied_with_file":          ("file",),
    "applied":                    ("outcome",),
    "postmortem_with_file":       ("file", "root_cause"),
    "postmortem":                 ("root_cause",),
    "observe_error":              ("file", "error_summary"),
    "cross_repo_impact":          ("file", "repo", "affected_count", "other_repos"),
    "duplication_blocked":   ("file",),
    "similarity_escalated":  ("file",),
    "verify_regression":     ("file", "root_cause"),
}

# Values that indicate a field was never populated with real data.
_SENTINEL_VALUES = {None, "", "unknown", "?", 0}


def format_narration(phase: str, context: Dict[str, Any]) -> Optional[str]:
    """Format a narration message for the given phase.

    Returns None when required context is missing — the caller should
    skip narration rather than speak placeholder text.
    """
    effective_phase = _resolve_template(phase, context)
    template = SCRIPTS.get(effective_phase, _FALLBACK)

    # Gate: refuse to narrate if critical fields are missing
    required = _REQUIRED_KEYS.get(effective_phase, ())
    for key in required:
        val = context.get(key)
        if val in _SENTINEL_VALUES:
            return None

    safe_ctx: Dict[str, Any] = {
        "s": "s",
        "phase": phase,
        "op_id": context.get("op_id", "unknown"),
        "file": "unknown",
        "provider": "unknown",
        "goal": "unknown",
        "outcome": "unknown",
        "root_cause": "unknown",
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
        return None


def _resolve_template(phase: str, context: Dict[str, Any]) -> str:
    """Pick the most specific template for the available context."""
    if phase == "signal_detected":
        tc = context.get("test_count")
        if tc is not None and tc not in _SENTINEL_VALUES:
            return "signal_detected_with_count"
        return "signal_detected"

    if phase == "applied":
        file_val = context.get("file")
        if file_val and file_val not in _SENTINEL_VALUES:
            return "applied_with_file"
        return "applied"

    if phase == "postmortem":
        file_val = context.get("file")
        if file_val and file_val not in _SENTINEL_VALUES:
            return "postmortem_with_file"
        return "postmortem"

    return phase

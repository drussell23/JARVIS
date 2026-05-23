"""Reflexive healing prompt formatter — Slice 12P Phase 3.

Composes structured ``<DEVELOPER_FEEDBACK>`` blocks from terminal-
rejection codes so the next GENERATE attempt sees an unambiguous
self-correction directive instead of just the raw error string.

The existing orchestrator retry-feedback path at
``orchestrator.py:5253`` already injects the rejection into the
next attempt's prompt. Slice 12P Phase 3 SHARPENS that injection
into a strict, structured developer-feedback block that:

  * names the rejection class explicitly (the model sees a
    closed-taxonomy signal, not a free-form error string)
  * states the rule violated in operator-binding language
    ("Validation Failed: You MUST...")
  * lists the canonical remediation steps as an action list
  * uses ``<CRITICAL_SYSTEM_OVERRIDE>``-class XML so the model's
    attention mechanism gives it priority over front-loaded task
    text (the existing pattern at orchestrator.py:5270+ comments
    this discipline at length)

Phase 3 is COMPOSITION ONLY — it does NOT run the LLM, does NOT
loop, does NOT manage retries. The orchestrator's existing
GENERATE retry loop (with Slice 12O cooldown + CostGovernor +
WallClockWatchdog already wired through it) provides the loop;
this module provides the SHAPE of what gets fed back.

NEVER raises. NEVER hardcodes prompt text outside the structured
formatter — the formatter composes a closed map of (rejection
class → remediation steps) so future rejection classes get
healing-prompt treatment automatically.
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.terminal_reason import (
    TerminalReasonClass,
    classify_terminal_reason,
    is_reflexive_healing_eligible,
)

logger = logging.getLogger("Ouroboros.ReflexiveHealing")


# Closed map of (root cause substring → canonical remediation
# action list). When a rejection's terminal_reason_code matches a
# substring here, the formatter emits the matched action list as
# the strict-instruction body of the developer-feedback block.
#
# AST-pinned: every STRUCTURAL_GATE_REJECTION classifier rule
# from terminal_reason.py MUST have a corresponding action list
# here. A new rejection class added to terminal_reason without a
# remediation entry trips the pin.

_REMEDIATION_ACTIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "exploration_insufficient",
        (
            "Call read_file on at least 2 of the target files BEFORE "
            "proposing any patch.",
            "Use search_code or get_callers to confirm which other "
            "modules import these targets.",
            "Only after you have inspected the actual code, return "
            "your patch as a JSON object matching the response schema.",
            "Do NOT return a patch on this attempt unless you have "
            "completed at least 2 exploration tool calls in the same "
            "tool loop.",
        ),
    ),
    (
        "ascii_gate_failed",
        (
            "Your previous patch contained non-ASCII codepoints in "
            "code positions (likely smart quotes, em-dashes, or other "
            "punctuation drift from streaming-render artifacts).",
            "Re-emit the patch using only ASCII punctuation. Replace "
            "every smart quote with a regular quote; every em-dash "
            "with a regular hyphen.",
            "Identifiers (function names, variable names) must contain "
            "only [A-Za-z0-9_].",
        ),
    ),
    (
        "semantic_guard_",
        (
            "SemanticGuardian flagged your patch as semantically unsafe "
            "(credential introduction, test assertion inversion, "
            "permission loosening, or function body collapse).",
            "Review the rejection detail below and re-emit the patch "
            "WITHOUT the flagged transformation.",
            "If the change is intentional, your patch must include an "
            "explicit explanation in the rationale field — the gate is "
            "treating it as accidental.",
        ),
    ),
    (
        "iron_gate_",
        (
            "An Iron Gate invariant rejected your patch. Read the "
            "rejection detail below.",
            "Address the specific invariant cited — do NOT submit the "
            "same patch shape on retry.",
            "Use tool calls to gather any additional context the "
            "rejection detail requests.",
        ),
    ),
    (
        "adversarial_reviewer_rejected",
        (
            "The Adversarial Reviewer found your patch incomplete or "
            "incorrect for the stated goal.",
            "Re-read the goal statement and the rejection detail "
            "below, then re-emit a complete patch.",
        ),
    ),
)


# Frozen XML wrapper. ``CRITICAL_SYSTEM_OVERRIDE`` is a tag the
# orchestrator already uses (see orchestrator.py:5285+ comments)
# to bypass attention bias toward front-loaded task description.
_FEEDBACK_HEADER: str = "<DEVELOPER_FEEDBACK priority=\"CRITICAL_SYSTEM_OVERRIDE\">"
_FEEDBACK_FOOTER: str = "</DEVELOPER_FEEDBACK>"


def format_structural_rejection_feedback(
    terminal_reason_code: str,
    rejection_detail: str = "",
    *,
    attempt_number: int = 0,
    max_attempts: int = 0,
) -> Optional[str]:
    """Compose a structured developer-feedback block for the
    given structural rejection.

    Returns the formatted XML block on success, or None if:
      * the rejection class isn't STRUCTURAL_GATE_REJECTION
        (provider exhaustion / wall cap / etc. — those get
        different recovery paths)
      * no remediation entry matches the rejection code (the
        AST pin should prevent this in production)

    Caller injects the returned string into the next GENERATE
    attempt's prompt prefix. The XML wrapper signals to the
    model's attention mechanism that this is a strict
    developer directive, not narrative context.

    NEVER raises.
    """
    if not is_reflexive_healing_eligible(terminal_reason_code):
        return None
    code_lower = (terminal_reason_code or "").lower()
    actions: Optional[Tuple[str, ...]] = None
    matched_class: Optional[str] = None
    for needle, action_list in _REMEDIATION_ACTIONS:
        if needle in code_lower:
            actions = action_list
            matched_class = needle
            break
    if actions is None:
        # No remediation entry — defensive None return; classifier
        # said it was structural-gate but we have no canonical
        # action list. Log so the AST pin catches the gap in
        # the next test run.
        logger.warning(
            "[ReflexiveHealing] no remediation entry for "
            "structural rejection: %s — formatter returning None",
            terminal_reason_code,
        )
        return None
    parts = [_FEEDBACK_HEADER]
    parts.append(
        f"  Validation Failed: rejection_class={matched_class!r}"
    )
    if attempt_number > 0 and max_attempts > 0:
        parts.append(
            f"  attempt={attempt_number}/{max_attempts}"
        )
    parts.append(f"  rejection_detail: {rejection_detail or '(none provided)'}")
    parts.append("")
    parts.append("  REQUIRED ACTIONS for your next attempt:")
    for i, action in enumerate(actions, start=1):
        parts.append(f"    {i}. {action}")
    parts.append("")
    parts.append(
        "  This is a structured developer-feedback directive. "
        "Your next response will be re-validated against the same "
        "gate. Submit a response that satisfies the REQUIRED "
        "ACTIONS list above."
    )
    parts.append(_FEEDBACK_FOOTER)
    return "\n".join(parts)


def get_remediation_actions(
    terminal_reason_code: str,
) -> Optional[Tuple[str, ...]]:
    """Return the canonical remediation action tuple for the
    given rejection, or None if no entry matches. Useful for
    telemetry / summary.json attribution. NEVER raises."""
    if not isinstance(terminal_reason_code, str) or not terminal_reason_code:
        return None
    code_lower = terminal_reason_code.lower()
    for needle, action_list in _REMEDIATION_ACTIONS:
        if needle in code_lower:
            return action_list
    return None


__all__ = [
    "format_structural_rejection_feedback",
    "get_remediation_actions",
]

"""
RecoveryFormatter — three renderings of a :class:`RecoveryPlan`.
==================================================================

Slice 2 of the Recovery Guidance + Voice Loop Closure arc. Pure
rendering helpers — the advisor produces the plan, the formatter
shapes it for the consumer:

* :func:`render_text` — SerpentFlow / REPL readable block.
* :func:`render_voice` — TTS-safe short phrasing (no markdown, no
  long commands, safe punctuation). Karen voice reads it.
* :func:`render_json` — structured payload for IDE observability
  (already provided by :meth:`RecoveryPlan.project`, but exposed
  here so consumers have one uniform import surface).

Authority boundary
------------------

* §1 read-only — stateless pure functions.
* §8 observable — every render is deterministic given the same plan.
* No imports from orchestrator / policy / iron_gate / risk_tier_floor /
  semantic_guardian / tool_executor / candidate_generator /
  change_engine. Grep-pinned at graduation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from backend.core.ouroboros.governance.recovery_advisor import (
    RECOVERY_PLAN_SCHEMA_VERSION,
    RecoveryPlan,
    RecoverySuggestion,
)

RECOVERY_FORMATTER_SCHEMA_VERSION: str = "recovery_formatter.v1"


# ---------------------------------------------------------------------------
# TTS-safety helpers
# ---------------------------------------------------------------------------


# macOS ``say`` chokes on long tokens / odd punctuation; keep the
# spoken output short and narrative. These regexes strip the noisy
# parts of programmer-facing strings so the voice version flows.
_UNSAFE_TTS_CHARS = re.compile(r"[`~<>{}|*\\]")
_MULTI_SPACE = re.compile(r"\s+")
_COMMAND_PATTERN = re.compile(
    r"(`[^`]+`|\$\{?[A-Z_][A-Z0-9_]*\}?|--[a-z][\w=./-]*|/[a-z][\w /-]*)"
)


def _tts_safe(text: str, *, max_len: int = 240) -> str:
    """Scrub a string for TTS.

    Strips backticks / angle brackets / pipes / asterisks / braces,
    collapses whitespace, and clips to ``max_len`` chars so Karen
    doesn't drone for minutes on one suggestion.
    """
    if not isinstance(text, str):
        text = str(text or "")
    text = _UNSAFE_TTS_CHARS.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "."
    return text


def _redact_command_for_voice(title: str) -> str:
    """Replace inline commands / env vars in a title with natural phrasing.

    Tiles like ``"Raise the per-op cost cap"`` stay readable; titles
    with embedded commands get cleaned up.
    """
    return _COMMAND_PATTERN.sub("that command", title)


# ---------------------------------------------------------------------------
# Text renderer — SerpentFlow / REPL
# ---------------------------------------------------------------------------


def render_text(plan: RecoveryPlan) -> str:
    """Render a plan for the REPL / SerpentFlow console.

    Example::

        Recovery for op-019abc...
          Cost cap reached at $0.80 / $0.50.
          Try next:
            1. [high] Inspect which phase ate the budget
               $ /cost op-019abc
               Why: Find the hot phase before spending more — the cost
                    may be concentrated in GENERATE or VERIFY...
            2. [medium] Raise the per-op cost cap
               $ JARVIS_OP_COST_BASELINE_USD=1.0
               Why: ...
    """
    if plan is None:
        return "  (no recovery plan)"
    lines: List[str] = [f"  Recovery for {plan.op_id or '<unknown>'}"]
    if plan.failure_summary:
        lines.append(f"    {plan.failure_summary}")
    if not plan.has_suggestions:
        lines.append("    (advisor produced no suggestions)")
        return "\n".join(lines)
    lines.append("    Try next:")
    for i, suggestion in enumerate(plan.suggestions, start=1):
        lines.append(
            f"      {i}. [{suggestion.priority}] {suggestion.title}"
        )
        if suggestion.command:
            lines.append(f"         $ {suggestion.command}")
        if suggestion.rationale:
            # Indent rationale so it aligns under the $ command line.
            wrapped = _wrap(suggestion.rationale, indent=11)
            lines.append(f"         Why: {wrapped}")
    return "\n".join(lines)


def _wrap(text: str, *, indent: int = 0, width: int = 72) -> str:
    """Simple word-wrap helper — avoids a textwrap dep for one call."""
    if len(text) <= width:
        return text
    words = text.split()
    out_lines: List[str] = []
    current: List[str] = []
    current_len = 0
    for word in words:
        added = len(word) + (1 if current else 0)
        if current_len + added > width and current:
            out_lines.append(" ".join(current))
            current = [word]
            current_len = len(word)
        else:
            current.append(word)
            current_len += added
    if current:
        out_lines.append(" ".join(current))
    pad = " " * indent
    return ("\n" + pad).join(out_lines)


# ---------------------------------------------------------------------------
# Voice renderer — Karen-safe
# ---------------------------------------------------------------------------


def render_voice(
    plan: RecoveryPlan,
    *,
    max_suggestions: int = 3,
    include_op_id: bool = False,
) -> str:
    """Render a plan as a short spoken narrative.

    * Drops command bodies (``$ grep...``) — commands don't narrate well.
    * Keeps priority words ("critical", "high") as natural phrasing.
    * Clips to ``max_suggestions`` (default 3) so Karen doesn't monologue.
    * Opt-in op_id inclusion — usually the operator knows which op from
      context; including the id reads like a serial number.
    """
    if plan is None or not plan.has_suggestions:
        return "No recovery suggestions available."
    parts: List[str] = []
    if include_op_id and plan.op_id:
        # Op ids are too long/random for comfortable TTS; take only
        # the short suffix.
        short = plan.op_id.split("-")[-1][:8]
        parts.append(f"For op ending {short},")
    if plan.failure_summary:
        parts.append(_tts_safe(plan.failure_summary))
    parts.append(_suggestion_count_phrase(plan.suggestions[:max_suggestions]))
    for i, s in enumerate(plan.suggestions[:max_suggestions], start=1):
        parts.append(_voice_for_suggestion(i, s))
    return _MULTI_SPACE.sub(" ", " ".join(parts)).strip()


def _suggestion_count_phrase(
    suggestions: "tuple[RecoverySuggestion, ...] | list[RecoverySuggestion]",
) -> str:
    n = len(suggestions)
    if n == 0:
        return ""
    if n == 1:
        return "Here is one thing to try."
    if n == 2:
        return "Here are two things to try."
    if n == 3:
        return "Here are three things to try."
    return f"Here are {n} things to try."


def _voice_for_suggestion(index: int, s: RecoverySuggestion) -> str:
    """Format one suggestion as a spoken sentence.

    Example: ``"First, inspect which phase ate the budget."``
    """
    ordinal = _ordinal(index)
    title = _redact_command_for_voice(s.title).rstrip(".")
    title = _tts_safe(title, max_len=120)
    return f"{ordinal}, {title}."


_ORDINALS = (
    "First", "Second", "Third", "Fourth", "Fifth",
    "Sixth", "Seventh", "Eighth", "Ninth", "Tenth",
)


def _ordinal(index: int) -> str:
    if 1 <= index <= len(_ORDINALS):
        return _ORDINALS[index - 1]
    return f"Suggestion {index}"


# ---------------------------------------------------------------------------
# JSON renderer — IDE observability
# ---------------------------------------------------------------------------


def render_json(plan: RecoveryPlan) -> Dict[str, Any]:
    """Return the plan's JSON projection.

    Identical to :meth:`RecoveryPlan.project` — exposed here so
    consumers have one place to import rendering APIs.
    """
    if plan is None:
        return {
            "schema_version": RECOVERY_PLAN_SCHEMA_VERSION,
            "has_plan": False,
            "suggestions": [],
        }
    projection = dict(plan.project())
    projection["has_plan"] = plan.has_suggestions
    return projection


__all__ = [
    "RECOVERY_FORMATTER_SCHEMA_VERSION",
    "render_json",
    "render_text",
    "render_voice",
]

"""P2 Slice 1 — IntentClassifier primitive.

Classifies operator natural-language input from SerpentFlow's
conversational REPL (PRD §9 Phase 3 P2) into one of four buckets so
``ConversationOrchestrator`` (Slice 2) can route appropriately:

  * ``ACTION_REQUEST`` — "do this now": synthesize a backlog entry +
    dispatch through the autonomous pipeline.
  * ``EXPLORATION`` — "find / understand X": spawn a read-only subagent.
  * ``EXPLANATION`` — "explain / why / what does …?": query Claude with
    relevant context (no mutation).
  * ``CONTEXT_PASTE`` — operator pasted code or an error trace; the
    orchestrator treats it as *additional context* for the previous
    turn's intent rather than a fresh classification, per PRD §9 P2
    edge-case spec.

This module is the **pure-data classifier** layer. It runs in <1ms,
ships zero LLM calls, and never touches the orchestrator / FSM / risk
engine. Slice 2 wires it behind the orchestrator dispatch; Slice 3
adds the SerpentFlow REPL surface; Slice 4 graduates the master flag.

Authority invariants (PRD §12.2):
  * Pure data — no I/O, no subprocess, no env mutation.
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Best-effort — degenerate inputs (empty / whitespace / oversize)
    are bucketed defensively. ``EXPLANATION`` is the safe default
    (no mutation surface) when no other signal matches.
  * Bounded — input is truncated to ``MAX_MESSAGE_CHARS`` before any
    pattern match so an enormous paste cannot pin the regex engine.

Ships default-off behind ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` —
Slice 4 will graduate it after the full chat surface is wired and
proven.
"""
from __future__ import annotations

import enum
import os
import re
from dataclasses import dataclass, field
from typing import Optional, Tuple


_TRUTHY = ("1", "true", "yes", "on")


# Per-message cap. Operator pastes can run to MB if they paste a giant
# log; the regex engine is fast but pinning a single-thread classifier
# on a 50-MB string is wasteful. Truncate first, then match.
MAX_MESSAGE_CHARS: int = 64 * 1024  # 64 KiB

# Code-paste heuristic thresholds — tunable but pinned by tests.
CODE_PASTE_MIN_NEWLINES: int = 3
CODE_PASTE_MIN_INDENT_LINES: int = 2

# When ``confidence`` falls below this floor, the orchestrator (Slice
# 2) MAY consult an LLM tiebreaker. Slice 1 ships pure-deterministic;
# the floor is exposed so future slices can layer on it.
LOW_CONFIDENCE_FLOOR: float = 0.40


def is_enabled() -> bool:
    """Master flag — ``JARVIS_CONVERSATIONAL_MODE_ENABLED`` (default
    **true** post Slice 4 graduation).

    Slices 1–3 shipped default-off (classifier + orchestrator +
    dispatcher all dormant). Slice 4 flipped the default after layered
    evidence: cross-slice authority pins + in-process live-fire smoke
    + factory-reachability supplement.

    When off, the SerpentFlow caller (and the
    :func:`build_chat_repl_dispatcher` factory) returns a no-op
    dispatcher — the chat surface is invisible to operators."""
    return os.environ.get(
        "JARVIS_CONVERSATIONAL_MODE_ENABLED", "1",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Intent enum + result dataclass
# ---------------------------------------------------------------------------


class ChatIntent(str, enum.Enum):
    """Routing target for one operator message."""

    ACTION_REQUEST = "ACTION_REQUEST"
    EXPLORATION = "EXPLORATION"
    EXPLANATION = "EXPLANATION"
    CONTEXT_PASTE = "CONTEXT_PASTE"


@dataclass(frozen=True)
class IntentClassification:
    """One classifier verdict.

    ``intent`` is the routing target; ``confidence`` is a 0..1 number
    derived from the count of distinct positive signals (rough — Slice
    2 may layer LLM tiebreaker for low-confidence verdicts);
    ``reasons`` lists the regex / heuristic rule names that fired so
    the operator can audit the verdict via ``/chat why`` (Slice 3)."""

    intent: ChatIntent
    confidence: float
    reasons: Tuple[str, ...] = field(default_factory=tuple)
    truncated: bool = False


# ---------------------------------------------------------------------------
# Pattern catalogue
# ---------------------------------------------------------------------------


# Compiled once. Using ``\b`` boundaries so "fix" matches "fix the bug"
# but not "fixture". Patterns are CASE-INSENSITIVE.
_ACTION_VERBS = re.compile(
    r"\b("
    r"add|build|create|generate|implement|write|"
    r"refactor|rename|extract|move|"
    r"fix|patch|repair|resolve|"
    r"delete|remove|drop|"
    r"update|upgrade|bump|migrate|"
    r"merge|land|ship|"
    r"run|execute|kick off|trigger|"
    r"deploy|release|"
    r"commit|stage|push|"
    r"replace"
    r")\b",
    re.IGNORECASE,
)

# "Make / can you / could you" softeners — slight ACTION lean when
# combined with an action verb later in the message.
_ACTION_SOFTENERS = re.compile(
    r"\b(make|can you|could you|please|let'?s|i need you to)\b",
    re.IGNORECASE,
)

_EXPLORATION_VERBS = re.compile(
    r"\b("
    r"find|search|grep|locate|hunt|"
    r"list|show me|enumerate|"
    r"explore|inspect|investigate|"
    r"audit|review|scan|"
    r"analyze|trace|follow|"
    r"check|verify|confirm"
    r")\b",
    re.IGNORECASE,
)

_EXPLANATION_VERBS = re.compile(
    r"\b("
    r"explain|describe|tell me|"
    r"why|how does|how did|what does|what is|what are|"
    r"summarize|recap|"
    r"compare|contrast|"
    r"document|clarify"
    r")\b",
    re.IGNORECASE,
)

# Question shape — leading "?" or trailing "?" or starts with WH-word.
_QUESTION_SHAPE = re.compile(
    r"^\s*(why|how|what|when|where|which|who|is\s|are\s|does\s|do\s|"
    r"can\s|could\s|should\s|would\s|will\s)",
    re.IGNORECASE,
)

# Leading-position explanation verb — strong signal that the operator
# is asking for an explanation even when an action verb follows
# ("explain how to fix the bug" → operator wants the explanation, not
# the fix).
_LEADING_EXPLANATION = re.compile(
    r"^\s*("
    r"explain|describe|tell me|summarize|recap|"
    r"compare|contrast|document|clarify"
    r")\b",
    re.IGNORECASE,
)

# Stack-trace / error markers (CONTEXT_PASTE signal).
_STACKTRACE_MARKERS = re.compile(
    r"("
    r"Traceback \(most recent call last\)|"
    r"\bat \w+(\.\w+)+\(.*?:\d+\)|"  # Java/JS stack frame
    r"\s+at\s+[\w./<>]+:\d+:\d+|"     # Node-style file:line:col
    r"^\s*File \".*?\", line \d+|"    # Python frame
    r"\b(Error|Exception|Panic):"
    r")",
    re.MULTILINE,
)

# Indent detection — count lines starting with 2+ spaces or a tab.
_INDENT_LINE = re.compile(r"^[ \t]{2,}\S", re.MULTILINE)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify(message: str) -> IntentClassification:
    """Classify one operator message into a :class:`ChatIntent`.

    Defensive defaults:
      * Empty / whitespace input → ``EXPLANATION`` confidence 0.0
        (safe — no mutation surface). Caller (Slice 2) can choose to
        treat this as a no-op.
      * Oversize input → truncated to ``MAX_MESSAGE_CHARS`` before
        matching; the verdict carries ``truncated=True``.
      * Code-paste heuristic fires FIRST (before verb matching) so an
        operator pasting a stack trace doesn't get misrouted as
        EXPLANATION because the trace contains the word "what".
    """
    if message is None:
        return IntentClassification(
            intent=ChatIntent.EXPLANATION, confidence=0.0,
            reasons=("none_input",),
        )
    text = str(message)
    truncated = len(text) > MAX_MESSAGE_CHARS
    if truncated:
        text = text[:MAX_MESSAGE_CHARS]
    if not text.strip():
        return IntentClassification(
            intent=ChatIntent.EXPLANATION, confidence=0.0,
            reasons=("empty",), truncated=truncated,
        )

    # ---- CONTEXT_PASTE first ----
    paste_reasons = _detect_context_paste(text)
    if paste_reasons:
        # Confidence scales with how many paste signals fired.
        conf = min(1.0, 0.50 + 0.20 * (len(paste_reasons) - 1))
        return IntentClassification(
            intent=ChatIntent.CONTEXT_PASTE,
            confidence=conf,
            reasons=tuple(paste_reasons),
            truncated=truncated,
        )

    # ---- Tally verb signals ----
    action_hit = bool(_ACTION_VERBS.search(text))
    softener_hit = bool(_ACTION_SOFTENERS.search(text))
    exploration_hit = bool(_EXPLORATION_VERBS.search(text))
    explanation_hit = bool(_EXPLANATION_VERBS.search(text))
    question_shape = bool(_QUESTION_SHAPE.match(text))
    leading_explanation = bool(_LEADING_EXPLANATION.match(text))

    scores = {
        ChatIntent.ACTION_REQUEST: (
            (1.0 if action_hit else 0.0)
            + (0.25 if softener_hit and action_hit else 0.0)
        ),
        ChatIntent.EXPLORATION: 1.0 if exploration_hit else 0.0,
        ChatIntent.EXPLANATION: (
            (0.7 if explanation_hit else 0.0)
            + (0.5 if question_shape else 0.0)
            # Leading "explain ..." dominates — the operator literally
            # asked for an explanation; anything that follows is the
            # subject, not a competing intent.
            + (0.8 if leading_explanation else 0.0)
        ),
    }

    # Pick winner; ties broken by safety order
    # (EXPLANATION > EXPLORATION > ACTION_REQUEST). EXPLANATION wins
    # ties because it has no mutation surface — false-positive cost
    # is "operator gets a wordy answer" instead of "we mutated files".
    safe_order = [
        ChatIntent.EXPLANATION,
        ChatIntent.EXPLORATION,
        ChatIntent.ACTION_REQUEST,
    ]
    winner = max(
        safe_order,
        key=lambda i: (scores[i], -safe_order.index(i)),
    )
    raw_score = scores[winner]
    if raw_score <= 0.0:
        # No signals fired — default to EXPLANATION at floor confidence.
        return IntentClassification(
            intent=ChatIntent.EXPLANATION, confidence=0.0,
            reasons=("no_signal_default",),
            truncated=truncated,
        )
    # Normalize confidence to 0..1; clamp.
    confidence = min(1.0, raw_score / 1.5)

    reasons = []
    if winner is ChatIntent.ACTION_REQUEST:
        if action_hit:
            reasons.append("action_verb")
        if softener_hit:
            reasons.append("action_softener")
    elif winner is ChatIntent.EXPLORATION:
        if exploration_hit:
            reasons.append("exploration_verb")
    elif winner is ChatIntent.EXPLANATION:
        if leading_explanation:
            reasons.append("leading_explanation")
        if explanation_hit and not leading_explanation:
            reasons.append("explanation_verb")
        if question_shape:
            reasons.append("question_shape")

    return IntentClassification(
        intent=winner, confidence=confidence,
        reasons=tuple(reasons), truncated=truncated,
    )


def _detect_context_paste(text: str) -> list:
    """Return a list of fired paste-signal names. Empty list = not a
    paste."""
    reasons = []
    if _STACKTRACE_MARKERS.search(text):
        reasons.append("stacktrace_marker")
    newline_count = text.count("\n")
    if newline_count >= CODE_PASTE_MIN_NEWLINES:
        indent_lines = len(_INDENT_LINE.findall(text))
        if indent_lines >= CODE_PASTE_MIN_INDENT_LINES:
            reasons.append("multiline_indented")
    # Triple-backtick fenced block is a near-certain paste signal.
    if "```" in text:
        reasons.append("fenced_code_block")
    return reasons


def is_low_confidence(verdict: IntentClassification) -> bool:
    """Caller convenience — Slice 2 may layer an LLM tiebreaker for
    these. Pinned for stability."""
    return verdict.confidence < LOW_CONFIDENCE_FLOOR


__all__ = [
    "CODE_PASTE_MIN_INDENT_LINES",
    "CODE_PASTE_MIN_NEWLINES",
    "ChatIntent",
    "IntentClassification",
    "LOW_CONFIDENCE_FLOOR",
    "MAX_MESSAGE_CHARS",
    "classify",
    "is_enabled",
    "is_low_confidence",
]
